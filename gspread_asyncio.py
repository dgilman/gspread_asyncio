import asyncio
import datetime
import functools
import logging

import gspread
from gspread.utils import extract_id_from_url, a1_to_rowcol
from gspread import Cell
import requests

# Copyright 2018 David Gilman
# Licensed under the MIT license. See LICENSE for details.

# Things this does:
# asyncio coroutine API (underlying gspread calls are run in the default threadpool executor)
# caching of gspread client/spreadsheet/worksheet objects
# automatic reauthorization (creds expire after 1 hr)
# automatic retries of failed request (spurious 500s from google)
# throttling of API calls to not go over api limits (1 call per sec)
# caching on the update_cell method to have it batch and flush updates

# By default when you await on a method in this library
# your caller will pause until the method finishes.
# Methods that don't return any value have an optional 'nowait' kwarg
# that schedules the coroutine for later execution on the event loop
# and returns control to your caller immediately.

# Usage:
# see bottom, but the tl;dr is:
# create a AsyncioGspreadClientManager
# when you need to do stuff with your spreadsheet, call AsyncioGspreadClientManager.authorize() to get a gspread client
# don't keep this client/its children around for more than an hour, but you can keep them around for a bit
# you can call authorize() (and other stuff like get_worksheet) often as their results are cached

# Methods decorated with nowait take an optional kwarg, 'nowait'
# If it is true, the method call gets scheduled on the event loop and
# returns a task.
def nowait(f):
   @functools.wraps(f)
   async def wrapper(*args, **kwargs):
      if 'nowait' not in kwargs:
         return await f(*args, **kwargs)
      nowait = kwargs['nowait']
      del kwargs['nowait']
      if nowait:
         return asyncio.ensure_future(f(*args, **kwargs), loop=args[0].agcm.loop)
      else:
         return await f(*args, **kwargs)
   return wrapper

class AsyncioGspreadClientManager(object):
   def __init__(self, credentials_fn, gspread_delay=1.1, cell_flush_delay=5,
      reauth_interval=45, loop=None, logger=None):
      self.credentials_fn = credentials_fn
      if loop == None:
         loop = asyncio.get_event_loop()
      self.loop = loop

      if logger == None:
         logger = logging.getLogger()
      self.l = logger

      # seconds
      self.gspread_delay = gspread_delay
      self.cell_flush_delay = cell_flush_delay
      # minutes
      self.reauth_interval = reauth_interval
      self.reauth_delta = datetime.timedelta(minutes=reauth_interval)

      self.agc_cache = {}
      self.auth_time = None
      self.auth_lock = asyncio.Lock(loop=self.loop)
      self.last_call = None
      self.call_lock = asyncio.Lock(loop=self.loop)

      self.dirty_worksheets = []
      self.cell_flusher_active = False

   async def _call(self, method, *args, **kwargs):
      while True:
         await self.call_lock.acquire()
         try:
            fn = functools.partial(method, *args, **kwargs)
            await self._delay()
            await self._before_gspread_call(method, args, kwargs)
            rval = await self.loop.run_in_executor(None, fn)
            return rval
         except gspread.exceptions.APIError as e:
            code = e.response.status_code
            # https://cloud.google.com/apis/design/errors
            # HTTP 400 range codes are errors that the caller should handle.
            # 429, however, is the rate limiting.
            # Catch it here, because we have handling and want to retry that one anyway.
            if code >= 400 and code <= 499 and code != 429:
               raise
            await self._handle_gspread_error(e, method, args, kwargs)
         except requests.RequestException as e:
            await self._handle_requests_error(e, method, args, kwargs)
         finally:
            self.call_lock.release()

   async def _before_gspread_call(self, method, args, kwargs):
      # I subclass this for custom logging
      self.l.debug("Calling {0} {1} {2}".format(method.__name__, str(args), str(kwargs)))

   async def _handle_gspread_error(self, e, method, args, kwargs):
      # By default, retry forever because sometimes Google just poops out and gives us a 500.
      # Subclass this to get custom error handling, backoff, jitter,
      # maybe even some cancellation
      self.l.exception("Error while calling {0} {1} {2}".format(method.__name__, str(args), str(kwargs)))
      # Wait a little bit just to keep from pounding Google
      await asyncio.sleep(self.gspread_delay)

   async def _handle_requests_error(self, e, method, args, kwargs):
      # By default, retry forever.
      self.l.exception("Error while calling {0} {1} {2}".format(method.__name__, str(args), str(kwargs)))
      # Wait a little bit just to keep from pounding Google
      await asyncio.sleep(self.gspread_delay)

   async def _delay(self):
      # Subclass this to customize rate limiting
      now = datetime.datetime.utcnow()
      if self.last_call == None:
         self.last_call = now
         return
      delta = (now - self.last_call)
      if delta.total_seconds() >= self.gspread_delay:
         self.last_call = now
         return
      delay_sec = float(delta.microseconds) / 10**6
      await asyncio.sleep(self.gspread_delay - delay_sec, loop=self.loop)
      self.last_call = datetime.datetime.utcnow()
      return

   async def authorize(self):
      await self.auth_lock.acquire()
      try:
         return await self._authorize()
      finally:
         self.auth_lock.release()

   async def _authorize(self):
      now = datetime.datetime.utcnow()
      if self.auth_time == None or self.auth_time + self.reauth_delta < now:
         creds = await self.loop.run_in_executor(None, self.credentials_fn)
         gc = await self.loop.run_in_executor(None, gspread.authorize, creds)
         agc = AsyncioGspreadClient(self, gc)
         self.agc_cache[now] = agc
         if self.auth_time in self.agc_cache:
            del self.agc_cache[self.auth_time]
         self.auth_time = now
      else:
         agc = self.agc_cache[self.auth_time]
      return agc

   async def _flush_dirty_worksheets(self):
      if len(self.dirty_worksheets) == 0:
         self.cell_flusher_active = False
         return

      # Don't call async in this critical section.
      # If interrupted, something could add to dirty_cells/dirty_worksheets
      # and the new addition would be lost.
      batch_updates = []
      for dws in self.dirty_worksheets:
         batch_updates.append((dws, [Cell(row, col, value) for row, col, value in dws.dirty_cells]))
         dws.dirty_cells = []
      self.dirty_worksheets = []

      for dws, cells in batch_updates:
         try:
            await dws.update_cells(cells)
         except gspread.exceptions.GSpreadException as e:
            await self._handle_dirty_worksheet_errors(dws, cells, e)

      # Reschedule self one last time in case we got raced during the above update_cells call.
      # The followup will terminate immediately if there's nothing to do so it's pretty harmless.
      self.loop.call_later(self.cell_flush_delay,
         lambda: asyncio.ensure_future(self._flush_dirty_worksheets(), loop=self.loop))

   async def _handle_dirty_worksheet_errors(self, dws, cells, e):
      # By default, log an error and move on.
      # Subclass this to get custom logging or error handling.
      self.l.exception('Error while flushing dirty cells: {0}'.format(e))

class AsyncioGspreadClient(object):
   def __init__(self, agcm, gc):
      self.agcm = agcm
      self.gc = gc
      self.ss_cache_title = {}
      self.ss_cache_key = {}

   async def create(self, title):
      ss = await self.agcm._call(self.gc.create, title)
      ass = AsyncioGspreadSpreadsheet(self.agcm, ss)
      self.ss_cache_title[title] = ass
      self.ss_cache_key[ss.id] = ass
      return ass

   @nowait
   async def del_spreadsheet(self, file_id):
      if file_id in self.ss_cache:
         del self.ss_cache_key[file_id]
      return await self.agcm._call(self.gc.del_spreadsheet, file_id)

   @nowait
   async def import_csv(self, file_id, data):
      return await self.agcm._call(self.gc.import_csv, file_id, data)

   @nowait
   async def insert_permission(self, file_id, value, perm_type, role, notify=True, email_message=None):
      return await self.agcm._call(self.gc.insert_permission, file_id, value, perm_type, role, notify=True, email_message=None)

   async def list_permissions(self, file_id):
      return await self.agcm._call(self.gc.list_permissions, file_id)

   async def login(self):
      raise NotImplemented("Use AsyncioGspreadClientManager.authorize() to create a gspread client")

   async def open(self, title):
      if title in self.ss_cache_title:
         return self.ss_cache_title[title]
      ss = await self.agcm._call(self.gc.open, title)
      ass = AsyncioGspreadSpreadsheet(self.agcm, ss)
      self.ss_cache_title[title] = ass
      self.ss_cache_key[ss.id] = ass
      return ass

   async def open_by_key(self, key):
      if key in self.ss_cache_key:
         return self.ss_cache_key[key]
      ss = await self.agcm._call(self.gc.open_by_key, key)
      ass = AsyncioGspreadSpreadsheet(self.agcm, ss)
      self.ss_cache_title[await ass.get_title()] = ass
      self.ss_cache_key[key] = ass
      return ass

   async def open_by_url(self, url):
      ss_id = extract_id_from_url(url)
      return await self.open_by_key(ss_id)

   async def openall(self, title=None):
      sses = await self.agcm._call(self.gc.openall, title=title)
      asses = []
      for ss in sses:
         ass = AsyncioGspreadSpreadsheet(self.agcm, ss)
         self.ss_cache_title[await ass.get_title()] = ass
         self.ss_cache_key[ss.id] = ass
         asses.append(ass)
      return asses

   @nowait
   async def remove_permission(self, file_id, permission_id):
      return await self.agcm._call(self.gc.remove_permission, file_id, permission_id)

class AsyncioGspreadSpreadsheet(object):
   def __init__(self, agcm, ss):
      self.agcm = agcm
      self.ss = ss

      self.ws_cache_title = {}
      self.ws_cache_idx = {}

   @nowait
   async def add_worksheet(self, title, rows, cols):
      ws = await self.agcm._call(self.ss.add_worksheet, title, rows, cols)
      aws = AsyncioGspreadWorksheet(self.agcm, ws)
      self.ws_cache_title[ws.title] = aws
      self.ws_cache_idx[ws._properties['index']] = aws
      return aws

   @nowait
   async def del_worksheet(self, worksheet):
      if worksheet.ws.title in self.ws_cache_title:
         del self.ws_cache_title[worksheet.ws.title]
      if worksheet.ws.id in self.ws_cache_key:
         del self.ws_cache_idx[worksheet.ws._properties['index']]
      return await self.agcm._call(self.ss.del_worksheet, worksheet.ws)

   async def get_worksheet(self, index):
      if index in self.ws_cache_idx:
         return self.ws_cache_idx[index]
      ws = await self.agcm._call(self.ss.get_worksheet, index)
      aws = AsyncioGspreadWorksheet(self.agcm, ws)
      self.ws_cache_title[ws.title] = aws
      self.ws_cache_idx[ws._properties['index']] = aws
      return aws

   @property
   def id(self):
      return self.ss.id

   async def list_permissions(self):
      return await self.agcm._call(self.ss.list_permissions)

   @nowait
   async def remove_permissions(self, value, role='any'):
      return await self.agcm._call(self.ss.remove_permissions, value, role='any')

   @nowait
   async def share(self, value, perm_type, role, notify=True, email_message=None):
      return await self.agcm._call(self.ss.share, value, perm_type, role, notify=True, email_message=None)

   @property
   def sheet1(self):
      return self.ss.sheet1

   @property
   def title(self):
      raise NotImplemented('This does network i/o. Please use the async get_title() function instead.')

   async def get_title(self):
      return await self.agcm._call(getattr, self.ss, 'title')

   async def worksheet(self, title):
      if title in self.ws_cache_title:
         return self.ws_cache_title[title]
      ws = await self.agcm._call(self.ss.worksheet, title)
      aws = AsyncioGspreadWorksheet(self.agcm, ws)
      self.ws_cache_title[title] = aws
      self.ws_cache_idx[ws._properties['index']] = aws
      return aws

   async def worksheets(self):
      wses = await self.agcm._call(self.ss.worksheets)
      awses = []
      for ws in wses:
         aws = AsyncioGspreadWorksheet(self.agcm, ws)
         self.ws_cache_title[ws.title] = aws
         self.ws_cache_idx[ws._properties['index']] = aws
         awses.append(aws)
      return awses

class AsyncioGspreadWorksheet(object):
   def __init__(self, agcm, ws):
      self.agcm = agcm
      self.ws = ws
      self.dirty_cells = []

      # XXX the cached cell updater uses the default value_input_option
      # Might want to come up with a way of letting users set that

   async def acell(self, label, value_render_option='FORMATTED_VALUE'):
      return await self.agcm._call(self.ws.acell, label, value_render_option=value_render_option)

   @nowait
   async def add_cols(self, cols):
      return await self.agcm._call(self.ws.add_cols, cols)

   @nowait
   async def add_rows(self, rows):
      return await self.agcm._call(self.ws.add_rows, rows)

# It would be great if there was an append_rows,
# and someone even implemented it in a PR upstream
# But we do not have it yet.
   @nowait
   async def append_row(self, values, value_input_option='RAW'):
      return await self.agcm._call(self.ws.append_row, values, value_input_option=value_input_option)

   async def cell(self, row, col, value_render_option='FORMATTED_VALUE'):
      return await self.agcm._call(self.ws.cell, row, col, value_render_option=value_render_option)

   @nowait
   async def clear(self):
      return await self.agcm._call(self.ws.clear)

   @property
   def col_count(self):
      return self.ws.col_count

   async def col_values(self, col, value_render_option='FORMATTED_VALUE'):
      return await self.agcm._call(self.ws.col_values, col, value_render_option=value_render_option)

   @nowait
   async def delete_row(self, index):
      return await self.agcm._call(self.ws.delete_row, index)

   async def find(self, query):
      return await self.agcm._call(self.ws.find, query)

   async def findall(self, query):
      return await self.agcm._call(self.ws.findall, query)

   async def get_all_records(self, empty2zero=False, head=1, default_blank=''):
      return await self.agcm._call(self.ws.get_all_records, empty2zero=empty2zero, head=head, default_blank=default_blank)

   async def get_all_values(self):
      return await self.agcm._call(self.ws.get_all_values)

   @property
   def id(self):
      return self.ws.id

   @nowait
   async def insert_row(self, values, index=1, value_input_option='RAW'):
      return await self.agcm._call(self.ws.insert_row, values, index=index, value_input_option=value_input_option)

   async def range(self, *args, **kwargs):
      return await self.agcm._call(self.ws.range, *args, **kwargs)

   @nowait
   async def resize(self, rows=None, cols=None):
      return await self.agcm._call(self.ws.resize, rows=rows, cols=cols)

   @property
   def row_count(self):
      return self.ws.row_count

   async def row_values(self, row, value_render_option='FORMATTED_VALUE'):
      return await self.agcm._call(self.ws.row_values, row, value_render_option=value_render_option)

   @property
   def title(self):
      return self.ws.title

   @nowait
   async def update_acell(self, label, value):
      row, col = a1_to_rowcol(label)
      return await self.update_cell(row, col, value)

   @nowait
   async def update_cell(self, row, col, value):
      return await self.agcm._call(self.ws.update_cell, row, col, value)

      # Unfortunately, the caching here is broken
      # update_cells creates a range out of the cells you provide,
      # it's meant to be more like a fill of an area.
      # If you shove an arbitrary amount of cells in there it'll wind up
      # modifying cells outside of your range.
#  async def update_cell_batch(self, row, col, value):
#      self.dirty_cells.append((row, col, value))
#      if self not in self.agcm.dirty_worksheets:
#         self.agcm.dirty_worksheets.append(self)
#      if self.agcm.cell_flusher_active == True:
#         return
#      else:
#         self.agcm.cell_flusher_active = True
#         self.agcm.loop.call_later(self.agcm.cell_flush_delay,
#            lambda: asyncio.ensure_future(self.agcm._flush_dirty_worksheets(), loop=self.agcm.loop))

   @nowait
   async def update_cells(self, cell_list, value_input_option='RAW'):
      return await self.agcm._call(self.ws.update_cells, cell_list, value_input_option=value_input_option)

   async def update_title(self, title):
      raise NotImplemented("This breaks ws caching, could be implemented later")

async def run(agcm):
   gc = await agcm.authorize()
   ss = await gc.create('Test Spreadsheet')
   print('Spreadsheet URL: https://docs.google.com/spreadsheets/d/{0}'.format(ss.id))
   await gc.insert_permission(ss.id, None, perm_type='anyone', role='writer')

   ws = await ss.add_worksheet('My Test Worksheet', 10, 5)
   zero_ws = await ss.get_worksheet(0)

   for row in range(1,11):
      for col in range(1,6):
         val = '{0}/{1}'.format(row, col)
         await ws.update_cell(row, col, val+" ws")
         await zero_ws.update_cell(row, col, val+" zero ws")

   await asyncio.sleep(30)
   agcm.loop.stop()

if __name__ == "__main__":
   from oauth2client.service_account import ServiceAccountCredentials

   def get_creds():
      return ServiceAccountCredentials.from_json_keyfile_name('serviceacct_spreadsheet.json',
         ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive',
         'https://www.googleapis.com/auth/spreadsheets'])
   agcm = AsyncioGspreadClientManager(get_creds)

   loop = asyncio.get_event_loop()
   loop.set_debug(True)
   loop.create_task(run(agcm))
   loop.run_forever()
