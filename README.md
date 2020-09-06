# gspread_asyncio

An [asyncio wrapper](https://docs.python.org/3/library/asyncio.html) for [burnash's excellent Google Spreadsheet API library](https://github.com/burnash/gspread). `gspread_asyncio` isn't just a plain asyncio wrapper around the `gspread` API, it implements several useful and helpful features on top of those APIs. It's useful for long-running processes and one-off scripts.

Requires Python >= 3.5 because of its use of async/await syntax.

[![Documentation Status](https://readthedocs.org/projects/gspread-asyncio/badge/?version=latest)](https://gspread-asyncio.readthedocs.io/en/latest/?badge=latest) [![Build Status](https://travis-ci.org/dgilman/gspread_asyncio.svg?branch=master)](https://travis-ci.org/dgilman/gspread_asyncio)

## Features

* Complete async wrapping of the `gspread` API. All `gspread` API calls are run off the main thread in a threadpool executor.
* Internal caching and reuse of `gspread` `Client`/`Spreadsheet`/`Worksheet` objects.
* Automatic renewal of expired credentials.
* Automatic retries of spurious failures from Google's servers (HTTP 5xx).
* Automatic rate limiting with defaults set to Google's default API limits.
* Many methods that don't need to return a value can optionally return an already-scheduled `Future` (the `nowait` kwarg). You can ignore that future, allowing forward progress on your calling coroutine while the asyncio event loop schedules and runs the Google Spreadsheet API call at a later time for you.

## Example usage

```python
import asyncio

import gspread_asyncio
from oauth2client.service_account import ServiceAccountCredentials

# First, set up a callback function that fetches our credentials off the disk.
# gspread_asyncio needs this to re-authenticate when credentials expire.

def get_creds():
    # To obtain a service account JSON file, follow these steps:
    # https://gspread.readthedocs.io/en/latest/oauth2.html#for-bots-using-service-account
    return ServiceAccountCredentials.from_json_keyfile_name(
        "serviceacct_spreadsheet.json",
        [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets",
        ],
    )

# Create an AsyncioGspreadClientManager object which
# will give us access to the Spreadsheet API.

agcm = gspread_asyncio.AsyncioGspreadClientManager(get_creds)

# Here's an example of how you use the API:

async def example(agcm):
    # Always authorize first.
    # If you have a long-running program call authorize() repeatedly.
    agc = await agcm.authorize()

    ss = await agc.create("Test Spreadsheet")
    print("Spreadsheet URL: https://docs.google.com/spreadsheets/d/{0}".format(ss.id))
    print("Open the URL in your browser to see gspread_asyncio in action!")

    # Allow anyone with the URL to write to this spreadsheet.
    await agc.insert_permission(ss.id, None, perm_type="anyone", role="writer")

    # Create a new spreadsheet but also grab a reference to the default one.
    ws = await ss.add_worksheet("My Test Worksheet", 10, 5)
    zero_ws = await ss.get_worksheet(0)

    # Write some stuff to both spreadsheets.
    for row in range(1, 11):
        for col in range(1, 6):
            val = "{0}/{1}".format(row, col)
            await ws.update_cell(row, col, val + " ws")
            await zero_ws.update_cell(row, col, val + " zero ws")
    print("All done!")

# Turn on debugging if you're new to asyncio!
asyncio.run(example(agcm), debug=True)
```

## Observational notes and gotchas

* This module does not define its own exceptions, it propagates instances of `gspread.exceptions.GSpreadException`.
* Always call `AsyncioGspreadClientManager.authorize()`, `AsyncioGspreadClient.open_*()` and `AsyncioGspreadSpreadsheet.get_worksheet()` before doing any work on a spreadsheet. These methods keep an internal cache so it is painless to call them many times, even inside of a loop. This makes sure you always have a valid set of authentication credentials from Google.
* The only object you should store in your application is the `AsyncioGspreadClientManager` (`agcm`).
* There is a [bug in the underlying gspread library](https://github.com/burnash/gspread/issues/600) where the `Spreadsheet.title` property does I/O. I think this should be fixed at the gspread layer, but until then you may have issues from failed API calls when accessing the `.title` property.
* Right now the `gspread` library does not support bulk appends of rows or bulk changes of cells. When this is done `gspread_asyncio` will support batching of these Google API calls without any changes to the Python `gspread_asyncio` API.
* I came up with the default 1.1 second delay between API calls (the `gspread_delay` kwarg) after extensive experimentation. The official API rate limit is one call every second but however Google measures these things introduces a tiny bit of jitter that will get you rate blocked if you ride that limit exactly.
* Google's service reliability on these endpoints is surprisingly bad. There are frequent HTTP 500s and the retry logic will save your butt in long-running scripts or short, one-shot, one-off ones.
* Experimentation also found that Google's credentials expire after an hour and the default `reauth_interval` of 45 minutes takes care of that just fine.

## License

MIT
