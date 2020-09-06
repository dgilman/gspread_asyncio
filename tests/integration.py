import unittest
import os
import asyncio

import gspread_asyncio
from oauth2client.service_account import ServiceAccountCredentials

# N.B. you must use a new password each time you encrypt a new CREDS with openssl.
# this is to avoid reuse of IVs.
def get_creds():
    return ServiceAccountCredentials.from_json_keyfile_name(
        os.environ["CREDS"],
        [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets",
        ],
    )


def async_test(f):
    def wrapper(*args, **kwargs):
        loop = asyncio.get_event_loop()
        loop.set_debug(True)
        loop.run_until_complete(f(*args, **kwargs))
        loop.close()

    return wrapper


class Smoketest(unittest.TestCase):
    """Not a real unit test - let's just get some coverage that the thing works ok."""

    @async_test
    async def test_smoke(self):
        agcm = gspread_asyncio.AsyncioGspreadClientManager(get_creds, gspread_delay=3.1)

        agc = await agcm.authorize()
        self.assertIsInstance(agc, gspread_asyncio.AsyncioGspreadClient)

        ss = await agc.create("Smoketest Spreadsheet")
        print(
            "Spreadsheet URL: https://docs.google.com/spreadsheets/d/{0}".format(ss.id)
        )
        self.assertIsInstance(ss, gspread_asyncio.AsyncioGspreadSpreadsheet)
        self.assertEqual(1, len(agc._ss_cache_key))
        self.assertEqual(1, len(agc._ss_cache_title))
        self.assertEqual(ss, agc._ss_cache_key[ss.id])
        self.assertEqual(ss, agc._ss_cache_title[await ss.get_title()])

        await agc.insert_permission(ss.id, None, perm_type="anyone", role="writer")

        ws = await ss.add_worksheet("My Test Worksheet", 2, 2)
        self.assertIsInstance(ws, gspread_asyncio.AsyncioGspreadWorksheet)
        self.assertEqual(1, len(ss._ws_cache_idx))
        self.assertEqual(1, len(ss._ws_cache_title))
        self.assertEqual(ws, ss._ws_cache_idx[1])
        self.assertEqual(ws, ss._ws_cache_title[ws.title])

        for row in range(1, 3):
            for col in range(1, 3):
                val = "{0}/{1}".format(row, col)
                await ws.update_cell(row, col, val)
                cell = await ws.cell(row, col)
                self.assertEqual(cell.value, val)

        await ss.del_worksheet(ws)
        self.assertEqual(0, len(ss._ws_cache_idx))
        self.assertEqual(0, len(ss._ws_cache_title))

        ss_id = ss.ss.id
        await agc.del_spreadsheet(ss_id)

        self.assertEqual(0, len(agc._ss_cache_key))
        self.assertEqual(0, len(agc._ss_cache_title))
