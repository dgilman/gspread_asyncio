import unittest
import os
import asyncio

import gspread_asyncio
from oauth2client.service_account import ServiceAccountCredentials

# N.B. you must use a new password each time you encrypt a new CREDS with openssl.
# this is to avoid reuse of IVs.
def get_creds():
   return ServiceAccountCredentials.from_json_keyfile_name(os.environ['CREDS'],
      ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive',
      'https://www.googleapis.com/auth/spreadsheets'])

def async_test(f):
   def wrapper(*args, **kwargs):
      loop = asyncio.get_event_loop()
      loop.set_debug(True)
      loop.run_until_complete(f(*args, **kwargs))
      loop.close()
   return wrapper

class Smoketest(unittest.TestCase):
   def setUp(self):
      self.agcm = gspread_asyncio.AsyncioGspreadClientManager(get_creds,
         gspread_delay=3.1)

   def tearDown(self):
      # call the synchronous gspread.Client.del_spreadsheet
      if hasattr(self, 'ss') and hasattr(self, 'agc'):
         ss_id = self.ss.ss.id
         self.agc.gc.del_spreadsheet(ss_id)

   @async_test
   async def test_smoke(self):
      agc = await self.agcm.authorize()
      self.agc = agc
      self.assertIsInstance(agc, gspread_asyncio.AsyncioGspreadClient)


      ss = await self.agc.create('Smoketest Spreadsheet')
      self.ss = ss
      print('Spreadsheet URL: https://docs.google.com/spreadsheets/d/{0}'.format(ss.id))
      self.assertIsInstance(ss, gspread_asyncio.AsyncioGspreadSpreadsheet)

      await agc.insert_permission(ss.id, None, perm_type='anyone', role='writer')

      ws = await ss.add_worksheet('My Test Worksheet', 2, 2)
      self.assertIsInstance(ws, gspread_asyncio.AsyncioGspreadWorksheet)
      for row in range(1, 3):
         for col in range(1, 3):
            val = "{0}/{1}".format(row, col)
            await ws.update_cell(row, col, val)
            cell = await ws.cell(row, col)
            self.assertEqual(cell.value, val)
