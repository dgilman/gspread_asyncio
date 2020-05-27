import asyncio
import functools
import logging

import gspread
from gspread.utils import extract_id_from_url, a1_to_rowcol
from gspread import Cell
import requests

# Copyright 2018 David Gilman
# Licensed under the MIT license. See LICENSE for details.

# Methods decorated with nowait take an optional kwarg, 'nowait'
# If it is true, the method call gets scheduled on the event loop and
# returns a task.
def _nowait(f):
   @functools.wraps(f)
   async def wrapper(*args, **kwargs):
      if 'nowait' not in kwargs:
         return await f(*args, **kwargs)
      nowait = kwargs['nowait']
      del kwargs['nowait']
      if nowait:
         return asyncio.ensure_future(f(*args, **kwargs), loop=args[0].agcm._loop)
      else:
         return await f(*args, **kwargs)
   return wrapper

class AsyncioGspreadClientManager(object):
   """Users of :mod:`gspread_asyncio` should instantiate this class and store it for the duration of their program.

      :param credentials_fn: A (non-async) function that takes no arguments and returns a :class:`~oauth2client.service_account.ServiceAccountCredentials`. This function will be called as needed to obtain new credential objects to replace expired ones. It is called from a thread in the default asyncio :py:class:`~concurrent.futures.ThreadPoolExecutor` so your function must be threadsafe.
      :param gspread_delay: (optional) The default delay (in seconds) between calls to Google Spreadsheet's APIs. The default rate limit is 1 call per second, the default rate limit here of 1.1 seconds is a good choice as it allows for some variance that creeps into Google's rate limit calculations.
      :param reauth_interval: (optional) The default delay (in minutes) before the :class:`~gspread_asyncio.AsyncioGspreadClientManager` requests new credentials.
      :type reauth_interval: int
      :param loop: (optional) The asyncio event loop to use.
      :param cell_flush_delay: Currently unused
   """
   def __init__(self, credentials_fn, gspread_delay=1.1,
      reauth_interval=45, loop=None, cell_flush_delay=5):
      self.credentials_fn = credentials_fn
      if loop == None:
         loop = asyncio.get_event_loop()
      self._loop = loop

      # seconds
      self.gspread_delay = gspread_delay
      self.cell_flush_delay = cell_flush_delay
      # arg is minutes, the stored value is seconds
      self.reauth_interval = reauth_interval * 60

      self._agc_cache = {}
      self.auth_time = None
      self.auth_lock = asyncio.Lock(loop=self._loop)
      self.last_call = None
      self.call_lock = asyncio.Lock(loop=self._loop)

      self._dirty_worksheets = []
      self._cell_flusher_active = False

   async def _call(self, method, *args, **kwargs):
      while True:
         await self.call_lock.acquire()
         try:
            fn = functools.partial(method, *args, **kwargs)
            await self.delay()
            await self.before_gspread_call(method, args, kwargs)
            rval = await self._loop.run_in_executor(None, fn)
            return rval
         except gspread.exceptions.APIError as e:
            code = e.response.status_code
            # https://cloud.google.com/apis/design/errors
            # HTTP 400 range codes are errors that the caller should handle.
            # 429, however, is the rate limiting.
            # Catch it here, because we have handling and want to retry that one anyway.
            if code >= 400 and code <= 499 and code != 429:
               raise
            await self.handle_gspread_error(e, method, args, kwargs)
         except requests.RequestException as e:
            await self.handle_requests_error(e, method, args, kwargs)
         finally:
            self.call_lock.release()

   async def before_gspread_call(self, method, args, kwargs):
      """Called before invoking a :mod:`gspread` method. Optionally subclass this to implement custom logging, tracing, or modification of the method arguments.

      The default implementation logs the method name, args and kwargs.

      :param method: gspread class method to be invoked
      :param args: positional arguments for the gspread class method
      :param kwargs: keyword arguments for the gspread class method
      """
      logging.debug("Calling {0} {1} {2}".format(method.__name__, str(args), str(kwargs)))

   async def handle_gspread_error(self, e, method, args, kwargs):
      """Called in the exception handler for a :class:`gspread.exceptions.APIError`. Optionally subclass this to implement custom error handling, error logging, rate limiting, backoff, or jitter.

      The default implementation logs the error and sleeps for :attr:`gspread_delay` seconds. It does not throw an exception of its own so it keeps retrying failed requests forever.

      gspread throws an :class:`~gspread.exceptions.APIError` when an error is returned from the Google API. `Google has some documentation on their HTTP status codes <https://cloud.google.com/apis/design/errors>`_. gspread makes a :class:`requests.Response` object accessible at :attr:`e.response`.

      Note that the internal :meth:`_call` method which invokes this method will not do so for any HTTP 400 statuses. These are errors that arise from mistaken usage of the Google API and are fatal. The exception is status code 429, the rate limiting status, to let this code handle client-side rate limiting.

      :param e: Exception object thrown by gspread
      :type e: :class:`~gspread.exceptions.APIError`
      :param method: gspread class method called
      :param args: positional arguments for the gspread class method
      :param kwargs: keyword arguments for the gspread class method
      """
      # By default, retry forever because sometimes Google just poops out and gives us a 500.
      # Subclass this to get custom error handling, backoff, jitter,
      # maybe even some cancellation
      logging.error("Error while calling {0} {1} {2}. Sleeping for {3} seconds."\
         .format(method.__name__, str(args), str(kwargs), self.gspread_delay))
      # Wait a little bit just to keep from pounding Google
      await asyncio.sleep(self.gspread_delay)

   async def handle_requests_error(self, e, method, args, kwargs):
      """Called in the exception handler for a :class:`requests.RequestException`. Optionally subclass to implement custom error handling, error logging, rate limiting, backoff, or jitter.

      The default implementation logs the error and sleeps for :attr:`gspread_delay` seconds. It does not throw an exception of its own so it keeps retrying failed requests forever.

      gspread throws a :class:`~requests.RequestException` when a socket layer error occurs.

      :param e: Exception object thrown by gspread
      :type e: :class:`~requests.RequestException`
      :param method: gspread class method called
      :param args: positional arguments for the gspread class method
      :param kwargs: keyword arguments for the gspread class method
      """
      # By default, retry forever.
      logging.error("Error while calling {0} {1} {2}. Sleeping for {3} seconds."\
         .format(method.__name__, str(args), str(kwargs), self.gspread_delay))
      # Wait a little bit just to keep from pounding Google
      await asyncio.sleep(self.gspread_delay)

   async def delay(self):
      """Called before invoking a :mod:`gspread` class method. Optionally subclass this to implement custom rate limiting.

      The default implementation figures out the delta between the last Google API call and now and sleeps for the delta if it is less than :attr:`gspread_delay`.
      """
      # Subclass this to customize rate limiting
      now = self._loop.time()
      if self.last_call == None:
         self.last_call = now
         return
      delta = (now - self.last_call)
      if delta >= self.gspread_delay:
         self.last_call = now
         return
      await asyncio.sleep(self.gspread_delay - delta, loop=self._loop)
      self.last_call = self._loop.time()
      return

   async def authorize(self):
      """(Re)-authenticates an :class:`~gspread_asyncio.AsyncioGspreadClientManager`. You **must** call this method first to log in to the Google Spreadsheets API.

      Feel free to call this method often, even in a loop, as it caches Google's credentials and only re-authenticates when the credentials are nearing expiration.

      :returns: a ready-to-use :class:`~gspread_asyncio.AsyncioGspreadClient`
      """
      await self.auth_lock.acquire()
      try:
         return await self._authorize()
      finally:
         self.auth_lock.release()

   async def _authorize(self):
      now = self._loop.time()
      if self.auth_time == None or self.auth_time + self.reauth_interval < now:
         creds = await self._loop.run_in_executor(None, self.credentials_fn)
         gc = await self._loop.run_in_executor(None, gspread.authorize, creds)
         agc = AsyncioGspreadClient(self, gc)
         self._agc_cache[now] = agc
         if self.auth_time in self._agc_cache:
            del self._agc_cache[self.auth_time]
         self.auth_time = now
      else:
         agc = self._agc_cache[self.auth_time]
      return agc

class AsyncioGspreadClient(object):
   """An :mod:`asyncio` wrapper for :class:`gspread.Client`. You **must** obtain instances of this class from :meth:`gspread_asyncio.AsyncioGspreadClientManager.authorize`.
   """
   def __init__(self, agcm, gc):
      self.agcm = agcm
      self.gc = gc
      self._ss_cache_title = {}
      self._ss_cache_key = {}

   async def create(self, title):
      """Create a new Google Spreadsheet. Wraps :meth:`gspread.Client.create`.

      :param title: Human-readable name of the new spreadsheet.
      :type title: str
      """
      ss = await self.agcm._call(self.gc.create, title)
      ass = AsyncioGspreadSpreadsheet(self.agcm, ss)
      self._ss_cache_title[title] = ass
      self._ss_cache_key[ss.id] = ass
      return ass

   @_nowait
   async def del_spreadsheet(self, file_id):
      """Delete a Google Spreadsheet. Wraps :meth:`gspread.Client.del_spreadsheet`.

      :param file_id: Google's spreadsheet id
      :type file_id: str
      :param nowait: (optional) If true, return a scheduled future instead of waiting for the API call to complete.
      :type nowait: bool
      """
      if file_id in self._ss_cache_key:
         del self._ss_cache_key[file_id]
      return await self.agcm._call(self.gc.del_spreadsheet, file_id)

   @_nowait
   async def import_csv(self, file_id, data):
      """Upload a csv file and save its data into the first page of the Google Spreadsheet. Wraps :meth:`gspread.Client.import_csv`.

      :param file_id: Google's spreadsheet id
      :type file_id: str
      :param data: The CSV file
      :type data: str
      :param nowait: (optional) If true, return a scheduled future instead of waiting for the API call to complete.
      :type nowait: bool
      """
      return await self.agcm._call(self.gc.import_csv, file_id, data)

   @_nowait
   async def insert_permission(self, file_id, value, perm_type, role, notify=True, email_message=None):
      """Add new permission to a Google Spreadsheet. Wraps :meth:`gspread.Client.insert_permission`.

      :param file_id: Google's spreadsheet id
      :type file_id: str
      :param value: user or group e-mail address, domain name or None for ‘default’ type.
      :type value: str, None
      :param perm_time: Allowed values are: ``user``, ``group``, ``domain``, ``anyone``.
      :type perm_type: str
      :param role: the primary role for this user. Allowed values are: ``owner``, ``writer``, ``reader``.
      :type perm_type: str
      :param notify: (optional) Whether to send an email to the target user/domain.
      :type notify: bool
      :param email_message: (optional) The email to be sent if notify=True
      :type email_message: str
      :param nowait: (optional) If true, return a scheduled future instead of waiting for the API call to complete.
      :type nowait: bool
      """
      return await self.agcm._call(self.gc.insert_permission, file_id, value, perm_type, role, notify=notify, email_message=email_message)

   async def list_permissions(self, file_id):
      """List the permissions of a Google Spreadsheet. Wraps :meth:`gspread.Client.list_permissions`.

      :param file_id: Google's spreadsheet id
      :type file_id: str

      :returns: Some kind of object with permissions in it. I don't know, the author of gspread forgot to document it.
      """
      return await self.agcm._call(self.gc.list_permissions, file_id)

   async def login(self):
      raise NotImplemented("Use AsyncioGspreadClientManager.authorize() to create a gspread client")

   async def open(self, title):
      """Opens a Google Spreadsheet by title. Wraps :meth:`gspread.Client.open`.

      Feel free to call this method often, even in a loop, as it caches the underlying spreadsheet object.

      :param title: The title of the spreadsheet
      :type title: str
      :returns: :class:`~gspread_asyncio.AsyncioGspreadSpreadsheet`
      """
      if title in self._ss_cache_title:
         return self._ss_cache_title[title]
      ss = await self.agcm._call(self.gc.open, title)
      ass = AsyncioGspreadSpreadsheet(self.agcm, ss)
      self._ss_cache_title[title] = ass
      self._ss_cache_key[ss.id] = ass
      return ass

   async def open_by_key(self, key):
      """Opens a Google Spreadsheet by spreasheet id. Wraps :meth:`gspread.Client.open_by_key`.

      Feel free to call this method often, even in a loop, as it caches the underlying spreadsheet object.

      :param key: Google's spreadsheet id
      :type key: str
      :returns: :class:`~gspread_asyncio.AsyncioGspreadSpreadsheet`
      """
      if key in self._ss_cache_key:
         return self._ss_cache_key[key]
      ss = await self.agcm._call(self.gc.open_by_key, key)
      ass = AsyncioGspreadSpreadsheet(self.agcm, ss)
      self._ss_cache_title[await ass.get_title()] = ass
      self._ss_cache_key[key] = ass
      return ass

   async def open_by_url(self, url):
      """Opens a Google Spreadsheet from a URL. Wraps :meth:`gspread.Client.open_by_url`.

      Feel free to call this method often, even in a loop, as it caches the underlying spreadsheet object.

      :param url: URL to a Google Spreadsheet
      :type url: str
      :returns: :class:`~gspread_asyncio.AsyncioGspreadSpreadsheet`
      """
      ss_id = extract_id_from_url(url)
      return await self.open_by_key(ss_id)

   async def openall(self, title=None):
      """Open all available spreadsheets. Wraps :meth:`gspread.Client.openall`.

      Feel free to call this method often, even in a loop, as it caches the underlying spreadsheet objects.

      :param title: (optional) If specified can be used to filter spreadsheets by title.
      :type title: str
      :returns: :py:class:`list` of :class:`~gspread_asyncio.AsyncioGspreadSpreadsheet`
      """
      sses = await self.agcm._call(self.gc.openall, title=title)
      asses = []
      for ss in sses:
         ass = AsyncioGspreadSpreadsheet(self.agcm, ss)
         self._ss_cache_title[await ass.get_title()] = ass
         self._ss_cache_key[ss.id] = ass
         asses.append(ass)
      return asses

   @_nowait
   async def remove_permission(self, file_id, permission_id):
      """Delete permissions from a Google Spreadsheet. Wraps :meth:`gspread.Client.remove_permission`.

      :param file_id: Google's spreadsheet id
      :type file_id: str
      :param nowait: (optional) If true, return a scheduled future instead of waiting for the API call to complete.
      :type nowait: bool
      """
      return await self.agcm._call(self.gc.remove_permission, file_id, permission_id)

class AsyncioGspreadSpreadsheet(object):
   """An :mod:`asyncio` wrapper for :class:`gspread.models.Spreadsheet`. You **must** obtain instances of this class from :meth:`AsyncioGspreadClient.open`, :meth:`AsyncioGspreadClient.open_by_key`, :meth:`AsyncioGspreadClient.open_by_url`, or :meth:`AsyncioGspreadClient.openall`.
   """
   def __init__(self, agcm, ss: gspread.Spreadsheet):
      self.agcm = agcm
      self.ss = ss

      self._ws_cache_title = {}
      self._ws_cache_idx = {}

   def __repr__(self):
      return '<{0} id:{1}>'.format(self.__class__.__name__, self.ss.id)

   async def add_worksheet(self, title, rows, cols):
      """Add new worksheet (tab) to a spreadsheet. Wraps :meth:`gspread.models.Spreadsheet.add_worksheet`.

      :param title: Human-readable title for the new worksheet
      :type title: str
      :param rows: Number of rows for the new worksheet
      :type rows: int
      :param cols: Number of columns for the new worksheet
      :type cols: int
      :returns: :py:class:`~gspread_asyncio.AsyncioGspreadWorksheet`
      """
      ws = await self.agcm._call(self.ss.add_worksheet, title, rows, cols)
      aws = AsyncioGspreadWorksheet(self.agcm, ws)
      self._ws_cache_title[ws.title] = aws
      self._ws_cache_idx[ws._properties['index']] = aws
      return aws

   @_nowait
   async def del_worksheet(self, worksheet):
      """Delete a worksheet (tab) from a spreadsheet. Wraps :meth:`gspread.models.Spreadsheet.del_worksheet`.

      :param worksheet: An instance of :class:`gspread.models.Worksheet`
      :param nowait: (optional) If true, return a scheduled future instead of waiting for the API call to complete.
      :type nowait: bool
      """
      if worksheet.ws.title in self._ws_cache_title:
         del self._ws_cache_title[worksheet.ws.title]
      if worksheet.ws.id in self._ws_cache_key:
         del self._ws_cache_idx[worksheet.ws._properties['index']]
      return await self.agcm._call(self.ss.del_worksheet, worksheet.ws)

   async def get_worksheet(self, index):
      """Retrieves a worksheet (tab) from a spreadsheet by index number. Indexes start from zero. Wraps :meth:`gspread.models.Spreadsheet.get_worksheet`.

      Feel free to call this method often, even in a loop, as it caches the underlying worksheet object.

      :param index: Index of worksheet
      :type index: int
      :returns: :class:`AsyncioGspreadWorksheet`
      """
      if index in self._ws_cache_idx:
         return self._ws_cache_idx[index]
      ws = await self.agcm._call(self.ss.get_worksheet, index)
      aws = AsyncioGspreadWorksheet(self.agcm, ws)
      self._ws_cache_title[ws.title] = aws
      self._ws_cache_idx[ws._properties['index']] = aws
      return aws

   async def fetch_sheet_metadata(self, params=None):
      """

      .. versionadded:: 1.1
      """
      return await self.agcm._call(self.ss.fetch_sheet_metadata, params=params)

   @property
   def id(self):
      """:returns: Google's spreadsheet id.
      :rtype: str
      """
      return self.ss.id

   async def list_permissions(self):
      """List the permissions of a Google Spreadsheet. Wraps :meth:`gspread.models.Spreadsheet.list_permissions`.

      :returns: The gspread author forgot to document this
      """
      return await self.agcm._call(self.ss.list_permissions)

   @_nowait
   async def remove_permissions(self, value, role='any'):
      """Remove permissions from a user or domain. Wraps :meth:`gspread.models.Spreadsheet.remove_permissions`.

      :param value: User or domain to remove permissions from
      :type value: str
      :param role: (optional) Permission to remove. Defaults to all
                   permissions.
      :type role: str
      :param nowait: (optional) If true, return a scheduled future instead of waiting for the API call to complete.
      :type nowait: bool
      """
      return await self.agcm._call(self.ss.remove_permissions, value, role='any')

   @_nowait
   async def share(self, value, perm_type, role, notify=True, email_message=None):
      """Share the spreadsheet with other accounts. Wraps :meth:`gspread.models.Spreadsheet.share`.

      :param value: user or group e-mail address, domain name
                    or None for 'default' type.
      :type value: str, None
      :param perm_type: The account type.
             Allowed values are: ``user``, ``group``, ``domain``,
             ``anyone``.
      :type perm_type: str
      :param role: The primary role for this user.
             Allowed values are: ``owner``, ``writer``, ``reader``.
      :type role: str
      :param notify: (optional) Whether to send an email to the target user/domain.
      :type notify: str
      :param email_message: (optional) The email to be sent if notify=True
      :type email_message: str
      :param nowait: (optional) If true, return a scheduled future instead of waiting for the API call to complete.
      :type nowait: bool
		"""
      return await self.agcm._call(self.ss.share, value, perm_type, role, notify=True, email_message=None)

   @property
   def sheet1(self):
      """:returns: Shortcut property for getting the first worksheet.
      :rtype: str
      """
      return self.ss.sheet1

   @property
   def title(self):
      raise NotImplemented('This does network i/o. Please use the async get_title() function instead.')

   async def get_title(self):
      """:returns: Title of the spreadsheet.
      :rtype: str
      """
      return await self.agcm._call(getattr, self.ss, 'title')

   async def worksheet(self, title):
      """Gets a worksheet (tab) by title. Wraps :meth:`gspread.models.Spreadsheet.worksheet`.

      Feel free to call this method often, even in a loop, as it caches the underlying worksheet object.

      :param title: Human-readable title of the worksheet.
      :type title: str
      :returns: :class:`~gspread_asyncio.AsyncioGspreadWorksheet`
      """
      if title in self._ws_cache_title:
         return self._ws_cache_title[title]
      ws = await self.agcm._call(self.ss.worksheet, title)
      aws = AsyncioGspreadWorksheet(self.agcm, ws)
      self._ws_cache_title[title] = aws
      self._ws_cache_idx[ws._properties['index']] = aws
      return aws

   async def worksheets(self):
      """Gets all worksheets (tabs) in a spreadsheet. Wraps :meth:`gspread.models.Spreadsheet.worksheets`.

      Feel free to call this method often, even in a loop, as it caches the underlying worksheet objects.

      :returns: :py:class:`list` of :class:`~gspread_asyncio.AsyncioGspreadWorksheet`
      """
      wses = await self.agcm._call(self.ss.worksheets)
      awses = []
      for ws in wses:
         aws = AsyncioGspreadWorksheet(self.agcm, ws)
         self._ws_cache_title[ws.title] = aws
         self._ws_cache_idx[ws._properties['index']] = aws
         awses.append(aws)
      return awses

class AsyncioGspreadWorksheet(object):
   """An :mod:`asyncio` wrapper for :class:`gspread.models.Worksheet`. You **must** obtain instances of this class from :meth:`AsynctioGspreadSpreadsheet.add_worksheet`, :meth:`AsyncioGspreadSpreadsheet.get_worksheet`, :meth:`AsyncioGspreadSpreadsheet.worksheet`, or :meth:`AsyncioGspreadSpreadsheet.worksheets`.
   """
   def __init__(self, agcm, ws: gspread.Worksheet):
      self.agcm = agcm
      self.ws = ws
      self.dirty_cells = []

   def __repr__(self):
      return '<{0} id:{1}>'.format(self.__class__.__name__, self.ws.id)

      # XXX the cached cell updater uses the default value_input_option
      # Might want to come up with a way of letting users set that

   @_nowait
   async def batch_update(
           self, data, value_input_option=None, include_values_in_response=None
   ):
       """Sets values in one or more cell ranges of the sheet at once.

       Wraps :meth:`gspread.models.Worksheet.batch_update`.

       :param list data: List of dictionaries in the form of
           `{'range': '...', 'values': [[.., ..], ...]}` where `range`
           is a target range to update in A1 notation or a named range,
           and `values` is a list of lists containing new values.

           For input, supported value types are: bool, string, and double.
           Null values will be skipped. To set a cell to an empty value, set
           the string value to an empty string.

       :param str value_input_option: (optional) How the input data should be
       interpreted.

       Possible values are:

       RAW             The values the user has entered will not be parsed
                       and will be stored as-is.
       USER_ENTERED    The values will be parsed as if the user typed them
                       into the UI. Numbers will stay as numbers, but
                       strings may be converted to numbers, dates, etc.
                       following the same rules that are applied when
                       entering text into a cell via the Google Sheets UI.

       Examples::
           await worksheet.batch_update([{
               'range': 'A1:B1',
               'values': [['42', '43']],
           }, {
               'range': 'my_range',
               'values': [['44', '45']],
           }, {
               'range': 'A2:B2',
               'values': [['42', None]],
           }])
           # Note: named ranges are defined in the scope of
           # a spreadsheet, so even if `my_range` does not belong to
           # this sheet it is still updated

       .. versionadded:: 1.1
       """
       return await self.agcm._call(
          self.ws.batch_update,
          data,
          value_input_option=value_input_option,
          include_values_in_response=include_values_in_response
       )

   async def acell(self, label, value_render_option='FORMATTED_VALUE'):
      """Returns an instance of a :class:`gspread.models.Cell`. Wraps :meth:`gspread.models.Worksheet.acell`.

      :param label: Cell label in A1 notation
                    Letter case is ignored.
      :type label: str
      :param value_render_option: (optional) Determines how values should be
                                  rendered in the the output. See
                                  `ValueRenderOption`_ in the Sheets API.
      :type value_render_option: str
      """
      return await self.agcm._call(self.ws.acell, label, value_render_option=value_render_option)

   @_nowait
   async def add_cols(self, cols):
      """Adds columns to worksheet. Wraps :meth:`gspread.models.Worksheet.add_cols`.

      :param cols: Number of new columns to add.
      :type cols: int
      :param nowait: (optional) If true, return a scheduled future instead of waiting for the API call to complete.
      :type nowait: bool
      """
      return await self.agcm._call(self.ws.add_cols, cols)

   @_nowait
   async def add_rows(self, rows):
      """Adds rows to worksheet. Wraps :meth:`gspread.models.Worksheet.add_rows`.

      :param cols: Number of new rows to add.
      :type cols: int
      :param nowait: (optional) If true, return a scheduled future instead of waiting for the API call to complete.
      :type nowait: bool
      """
      return await self.agcm._call(self.ws.add_rows, rows)

   @_nowait
   async def append_row(self, values, value_input_option='RAW'):
      """Adds a row to the worksheet and populates it with values. Widens the worksheet if there are more values than columns. Wraps :meth:`gspread.models.Worksheet.append_row`.

      :param values: List of values for the new row.
      :param value_render_option: (optional) Determines how values should be
                                  rendered in the the output. See
                                  `ValueRenderOption`_ in the Sheets API.
      :type value_render_option: str
      :param nowait: (optional) If true, return a scheduled future instead of waiting for the API call to complete.
      :type nowait: bool

      .. _ValueInputOption: https://developers.google.com/sheets/api/reference/rest/v4/ValueInputOption
      """
      return await self.agcm._call(self.ws.append_row, values, value_input_option=value_input_option)

   @_nowait
   async def append_rows(
        self,
        values,
        value_input_option='RAW',
        insert_data_option=None,
        table_range=None,
   ):
       """Adds multiple rows to the worksheet and populates them with values.

       Widens the worksheet if there are more values than columns.

       NOTE: it doesn't extends filtered range.

       Wraps :meth:`gspread.models.Worksheet.append_rows`.

       :param list values: List of rows each row is List of values for
           the new row.
       :param str value_input_option: (optional) Determines how input data
           should be interpreted. See `ValueInputOption`_ in the Sheets API.
       :param str insert_data_option: (optional) Determines how the input data
           should be inserted. See `InsertDataOption`_ in the Sheets API
           reference.
       :param str table_range: (optional) The A1 notation of a range to search
           for a logical table of data. Values are appended after the last row
           of the table. Examples: `A1` or `B2:D4`

       .. versionadded:: 1.1

       .. _ValueInputOption: https://developers.google.com/sheets/api/reference/rest/v4/ValueInputOption
       .. _InsertDataOption: https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets.values/append#InsertDataOption
       """

       return await self.agcm._call(
          self.ws.append_rows,
          values,
          value_input_option=value_input_option,
          insert_data_option=insert_data_option,
          table_range=table_range,
       )

   async def cell(self, row, col, value_render_option='FORMATTED_VALUE'):
      """Returns an instance of a :class:`gspread.models.Cell` located at `row` and `col` column. Wraps :meth:`gspread.models.Worksheet.cell`.

      :param row: Row number.
      :type row: int
      :param col: Column number.
      :type col: int
      :param value_render_option: (optional) Determines how values should be
                                  rendered in the the output. See
                                  `ValueRenderOption`_ in the Sheets API.
      :type value_render_option: str

      .. _ValueRenderOption: https://developers.google.com/sheets/api/reference/rest/v4/ValueRenderOption
      """
      return await self.agcm._call(self.ws.cell, row, col, value_render_option=value_render_option)

   @_nowait
   async def clear(self):
      """Clears all cells in the worksheet. Wraps :meth:`gspread.models.Worksheet.clear`.

      :param nowait: (optional) If true, return a scheduled future instead of waiting for the API call to complete.
      :type nowait: bool
      """
      return await self.agcm._call(self.ws.clear)

   @property
   def col_count(self):
      """:returns: Number of columns in the worksheet.
      :rtype: int
      """
      return self.ws.col_count

   async def col_values(self, col, value_render_option='FORMATTED_VALUE'):
      """Returns a list of all values in column `col`. Wraps :meth:`gspread.models.Worksheet.col_values`.

      Empty cells in this list will be rendered as :const:`None`.

      :param col: Column number.
      :type col: int
      :param value_render_option: (optional) Determines how values should be
                                  rendered in the the output. See
                                  `ValueRenderOption`_ in the Sheets API.
      :type value_render_option: str
      """
      return await self.agcm._call(self.ws.col_values, col, value_render_option=value_render_option)

   @_nowait
   async def delete_row(self, index):
      """Deletes the row from the worksheet at the specified index. Wraps :meth:`gspread.models.Worksheet.delete_row`.

      :param index: Index of a row for deletion.
      :type index: int
      :param nowait: (optional) If true, return a scheduled future instead of waiting for the API call to complete.
      :type nowait: bool
      """
      return await self.agcm._call(self.ws.delete_row, index)

   @_nowait
   async def add_protected_range(
       self,
       name,
       editor_users_emails=None,
       editor_groups_emails=None,
       description=None,
       warning_only=None,
       requesting_user_can_edit=None,
   ):
       """Add protected range to the sheet. Only the editors can edit
       the protected range.

       :param str name: A string with range value in A1 notation,
           e.g. 'A1:A5'.

       Alternatively, you may specify numeric boundaries. All values
       index from 1 (one):

       :param int first_row: First row number
       :param int first_col: First column number
       :param int last_row: Last row number
       :param int last_col: Last column number

       :param list editor_users_emails: (optional) The email addresses of
           users with edit access to the protected range.
       :param list editor_groups_emails: (optional) The email addresses of
           groups with edit access to the protected range.
       :param str description: (optional) Description for the protected
           ranges.
       :param boolean warning_only: (optional) When true this protected range
           will show a warning when editing. Defaults to ``False``.
       :param boolean requesting_user_can_edit: (optional) True if the user
           who requested this protected range can edit the protected cells.
           Defaults to ``False``.

        .. versionadded:: 1.1
       """
       return await self.agcm._call(
          self.ws.add_protected_range,
          name,
          editor_users_emails=editor_users_emails,
          editor_groups_emails=editor_groups_emails,
          description=description,
          warning_only=warning_only,
          requesting_user_can_edit=requesting_user_can_edit,
       )

   async def find(self, query):
      """Finds the first cell matching the query. Wraps :meth:`gspread.models.Worksheet.find`.

      :param query: A literal string to match or compiled regular expression.
      :type query: str, :py:class:`re.Pattern`
      """
      return await self.agcm._call(self.ws.find, query)

   async def findall(self, query):
      """Finds all cells matching the query. Wraps :meth:`gspread.models.Worksheet.find`.

      :param query: A literal string to match or compiled regular expression.
      :type query: str, :py:class:`re.Pattern`
      """
      return await self.agcm._call(self.ws.findall, query)

   async def get_all_records(self, empty2zero=False, head=1, default_blank=''):
      """Returns a list of dictionaries, all of them having the contents
      of the spreadsheet with the head row as keys and each of these
      dictionaries holding the contents of subsequent rows of cells
      as values. Wraps :meth:`gspread.models.Worksheet.get_all_records`.

      Cell values are numericised (strings that can be read as ints
      or floats are converted).

      :param empty2zero: (optional) Determines whether empty cells are
                         converted to zeros.
      :type empty2zero: bool
      :param head: (optional) Determines wich row to use as keys, starting
                   from 1 following the numeration of the spreadsheet.
      :type head: int
      :param default_blank: (optional) Determines whether empty cells are
                            converted to something else except empty string
                            or zero.
      :type default_blank: str
      """
      return await self.agcm._call(self.ws.get_all_records, empty2zero=empty2zero, head=head, default_blank=default_blank)

   async def get_all_values(self):
      """Returns a list of lists containing all cells' values as strings. Wraps :meth:`gspread.models.Worksheet.get_all_values`.
      """
      return await self.agcm._call(self.ws.get_all_values)

   @property
   def id(self):
      """:returns: Google's spreadsheet id.
      :rtype: str
      """
      return self.ws.id

   @_nowait
   async def insert_row(self, values, index=1, value_input_option='RAW'):
      """Adds a row to the worksheet at the specified index
      and populates it with values. Wraps :meth:`gspread.models.Worksheet.insert_row`.

      Widens the worksheet if there are more values than columns.

      :param values: List of values for the new row.
      :param index: (optional) Offset for the newly inserted row.
      :type index: int
      :param value_render_option: (optional) Determines how values should be
                                  rendered in the the output. See
                                  `ValueRenderOption`_ in the Sheets API.
      :type value_render_option: str
      :param nowait: (optional) If true, return a scheduled future instead of waiting for the API call to complete.
      :type nowait: bool

      .. _ValueInputOption: https://developers.google.com/sheets/api/reference/rest/v4/ValueInputOption

      """
      return await self.agcm._call(self.ws.insert_row, values, index=index, value_input_option=value_input_option)

   @_nowait
   async def insert_rows(self, values, row=1, value_input_option='RAW'):
       """Adds multiple rows to the worksheet at the specified index and
       populates them with values.

       :param list values: List of row lists. a list of lists, with the lists
           each containing one row's values. Widens the worksheet if there are
           more values than columns.
       :param int row: Start row to update (one-based). Defaults to 1 (one).

       .. versionadded:: 1.1
       """
       return await self.agcm._call(
          self.ws.insert_rows,
          values,
          row=row,
          value_input_option=value_input_option,
       )

   async def range(self, *args, **kwargs):
      """Returns a list of :class:`Cell` objects from a specified range. Wraps :meth:`gspread.models.Worksheet.range`.

      :param name: A string with range value in A1 notation, e.g. 'A1:A5'.
      :type name: str

      Alternatively, you may specify numeric boundaries. All values
      index from 1 (one):

      :param first_row: Row number
      :type first_row: int
      :param first_col: Row number
      :type first_col: int
      :param last_row: Row number
      :type last_row: int
      :param last_col: Row number
      :type last_col: int
      """
      return await self.agcm._call(self.ws.range, *args, **kwargs)

   @_nowait
   async def resize(self, rows=None, cols=None):
      """Resizes the worksheet. Specify one of ``rows`` or ``cols``. Wraps :meth:`gspread.models.Worksheet.resize`.

      :param rows: (optional) New number of rows.
      :type rows: int
      :param cols: (optional) New number columns.
      :type cols: int
      :param nowait: (optional) If true, return a scheduled future instead of waiting for the API call to complete.
      :type nowait: bool
      """
      return await self.agcm._call(self.ws.resize, rows=rows, cols=cols)

   @property
   def row_count(self):
      """:returns: Number of rows in the worksheet.
      :rtype: int
      """
      return self.ws.row_count

   async def row_values(self, row, value_render_option='FORMATTED_VALUE'):
      """Returns a list of all values in a `row`. Wraps :meth:`gspread.models.Worksheet.row_values`.

      Empty cells in this list will be rendered as :const:`None`.

      :param row: Row number.
      :type row: int
      :param value_render_option: (optional) Determines how values should be
                                  rendered in the the output. See
                                  `ValueRenderOption`_ in the Sheets API.
      :type value_render_option: str

      .. _ValueRenderOption: https://developers.google.com/sheets/api/reference/rest/v4/ValueRenderOption

      """
      return await self.agcm._call(self.ws.row_values, row, value_render_option=value_render_option)

   @property
   def title(self):
      """:returns: Human-readable worksheet title.
      :rtype: str
      """
      return self.ws.title

   @_nowait
   async def update_acell(self, label, value):
      """Updates the value of a cell. Wraps :meth:`gspread.models.Worksheet.row_values`.

      :param label: Cell label in A1 notation.
                    Letter case is ignored.
      :type label: str
      :param value: New value.
      :param nowait: (optional) If true, return a scheduled future instead of waiting for the API call to complete.
      :type nowait: bool
      """
      row, col = a1_to_rowcol(label)
      return await self.update_cell(row, col, value)

   @_nowait
   async def update_cell(self, row, col, value):
      """Updates the value of a cell.

      :param row: Row number.
      :type row: int
      :param col: Column number.
      :type col: int
      :param value: New value.
      :param nowait: (optional) If true, return a scheduled future instead of waiting for the API call to complete.
      :type nowait: bool
      """
      return await self.agcm._call(self.ws.update_cell, row, col, value)

   @_nowait
   async def update_cells(self, cell_list, value_input_option='RAW'):
      """Updates many cells at once. Wraps :meth:`gspread.models.Worksheet.update_cells`.

      :param cell_list: List of :class:`~gspread.models.Cell` objects to update.
      :param value_render_option: (optional) Determines how values should be
                                  rendered in the the output. See
                                  `ValueRenderOption`_ in the Sheets API.
      :type value_render_option: str
      :param nowait: (optional) If true, return a scheduled future instead of waiting for the API call to complete.
      :type nowait: bool

      .. _ValueInputOption: https://developers.google.com/sheets/api/reference/rest/v4/ValueInputOption
      """
      return await self.agcm._call(self.ws.update_cells, cell_list, value_input_option=value_input_option)

   async def batch_get(
           self,
           ranges,
           major_dimension=None,
           value_render_option=None,
           date_time_render_option=None,
   ):
       """Returns one or more ranges of values from the sheet.

       :param list ranges: List of cell ranges in the A1 notation or named
           ranges.

       :param str major_dimension: (optional) The major dimension that results
           should use.

       :param str value_render_option: (optional) How values should be
           represented in the output. The default render option
           is `FORMATTED_VALUE`.

       :param str date_time_render_option: (optional) How dates, times, and
           durations should be represented in the output. This is ignored if
           value_render_option is FORMATTED_VALUE. The default dateTime render
           option is `SERIAL_NUMBER`.

       Examples::

           # Read values from 'A1:B2' range and 'F12' cell
           await worksheet.batch_get(['A1:B2', 'F12'])

       .. versionadded:: 1.1
       """
       return await self.agcm._call(
          self.ws.batch_get,
          ranges,
          major_dimension=major_dimension,
          value_render_option=value_render_option,
          date_time_render_option=date_time_render_option,
       )

   async def update_title(self, title):
      raise NotImplemented("This breaks ws caching, could be implemented later")

__all__ = [
   "AsyncioGspreadClientManager",
   "AsyncioGspreadClient",
   "AsyncioGspreadSpreadsheet",
   "AsyncioGspreadWorksheet"
]
