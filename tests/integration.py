import asyncio
import os
import unittest

from google.oauth2.service_account import Credentials
from oauth2client.service_account import ServiceAccountCredentials

import gspread_asyncio


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


def get_creds_google_oauth2():
    creds = Credentials.from_service_account_file(os.environ["CREDS"])
    scoped = creds.with_scopes(
        [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets",
        ],
    )
    return scoped


def async_test(f):
    def wrapper(*args, **kwargs):
        loop = asyncio.new_event_loop()
        loop.set_debug(True)
        loop.run_until_complete(f(*args, **kwargs))
        loop.close()

    return wrapper


class Smoketest(unittest.TestCase):
    """Not a real unit test - let's just get some coverage that the thing works ok."""

    async def _test_smoke(self, creds_fn):
        agcm = gspread_asyncio.AsyncioGspreadClientManager(creds_fn, gspread_delay=3.1)

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
        self.assertEqual(ss, agc._ss_cache_title[ss.title])

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

        self.assertEqual(await ws.get_values(), [["1/1", "1/2"], ["2/1", "2/2"]])

        cells = await ws.get_all_cells()
        self.assertEqual(
            {cell.value for cell in cells if cell.value},
            {"1/1", "1/2", "2/1", "2/2"},
        )
        self.assertEqual(await ws.expand("A1"), [["1/1", "1/2"], ["2/1", "2/2"]])

        rng = "'{0}'!A1:B2".format(ws.title)

        # values_batch_update overwrites the same block in a single call...
        await ss.values_batch_update(
            body={
                "valueInputOption": "RAW",
                "data": [{"range": rng, "values": [["a", "b"], ["c", "d"]]}],
            }
        )
        self.assertEqual(await ws.get_values(), [["a", "b"], ["c", "d"]])

        # ...and values_batch_clear wipes it back out.
        await ss.values_batch_clear(body={"ranges": [rng]})
        cleared = await ws.get_values()
        self.assertEqual([v for row in cleared for v in row if v], [])

        # is_gridlines_hidden: False by default, True after hiding. hide_gridlines
        # doesn't mutate the local properties, so re-fetch to observe the change.
        self.assertIs(ws.is_gridlines_hidden, False)
        await ws.hide_gridlines()
        ws_refetched = await ss.get_worksheet_by_id(ws.id)
        self.assertIs(ws_refetched.is_gridlines_hidden, True)

        await ws.freeze(rows=1, cols=1)
        self.assertEqual(1, ws.frozen_row_count)
        self.assertEqual(1, ws.frozen_col_count)

        ws_del = await ss.add_worksheet("Throwaway Worksheet", 1, 1)
        del_id = ws_del.id
        self.assertIn("Throwaway Worksheet", ss._ws_cache_title)
        del_idx = ws_del.ws._properties["index"]
        await ss.del_worksheet_by_id(del_id)
        self.assertNotIn("Throwaway Worksheet", ss._ws_cache_title)
        self.assertNotIn(del_idx, ss._ws_cache_idx)

        await ss.del_worksheet(ws)
        self.assertEqual(0, len(ss._ws_cache_idx))
        self.assertEqual(0, len(ss._ws_cache_title))

        ss_id = ss.ss.id
        await agc.del_spreadsheet(ss_id)

        self.assertEqual(0, len(agc._ss_cache_key))
        self.assertEqual(0, len(agc._ss_cache_title))

    @async_test
    async def test_smoke(self):
        await self._test_smoke(get_creds)

    @async_test
    async def test_smoke_creds_google_oauth2(self):
        await self._test_smoke(get_creds_google_oauth2)
