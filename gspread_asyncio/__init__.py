import asyncio
import functools
import logging
from typing import TYPE_CHECKING, Callable, Dict, Iterable, List, Optional, Tuple, Union

import gspread
from gspread.utils import a1_to_rowcol, extract_id_from_url
import requests

# Copyright 2018-2022 David Gilman
# Licensed under the MIT license. See LICENSE for details.

# Methods decorated with nowait take an optional kwarg, 'nowait'
# If it is true, the method call gets scheduled on the event loop and
# returns a task.

if TYPE_CHECKING:
    import re

    from google.auth.credentials import Credentials
    from oauth2client.client import (
        AccessTokenCredentials,
        GoogleCredentials,
        OAuth2Credentials,
    )
    from oauth2client.service_account import ServiceAccountCredentials

    CredentialTypes = Union[
        ServiceAccountCredentials,
        OAuth2Credentials,
        AccessTokenCredentials,
        GoogleCredentials,
        Credentials,
    ]


def _nowait(f):
    @functools.wraps(f)
    async def wrapper(*args, **kwargs):
        nowait = False
        if "nowait" in kwargs:
            nowait = kwargs["nowait"]
            del kwargs["nowait"]

        if nowait:
            return asyncio.create_task(f(*args, **kwargs))
        else:
            return await f(*args, **kwargs)

    return wrapper


class AsyncioGspreadClientManager(object):
    """Users of :mod:`gspread_asyncio` should instantiate this class and
    store it for the duration of their program.

    :param credentials_fn: A (non-async) function that takes no arguments
        and returns an instance of
        :class:`google.auth.credentials.Credentials` (preferred),
        :class:`oauth2client.service_account.ServiceAccountCredentials`,
        :class:`oauth2client.client.OAuth2Credentials`,
        :class:`oauth2client.client.AccessTokenCredentials`, or
        :class:`oauth2client.client.GoogleCredentials`.
        This function will be called as needed to obtain new credential
        objects to replace expired ones. It is called from a thread in
        the default asyncio
        :py:class:`~concurrent.futures.ThreadPoolExecutor` so your function
        must be threadsafe.
    :type credentials_fn: Callable
    :param float gspread_delay: (optional) The default delay (in seconds) between
        calls to Google Spreadsheet's APIs. The default rate limit is 1 call
        per second, the default rate limit here of 1.1 seconds is a good
        choice as it allows for some variance that creeps into Google's rate
        limit calculations.
    :param int reauth_interval: (optional) The default delay (in minutes)
        before the :class:`~gspread_asyncio.AsyncioGspreadClientManager`
        requests new credentials.
    :param loop: (optional) The asyncio event loop to use.
    :type loop: :py:class:`~asyncio.AbstractEventLoop`
    :param float cell_flush_delay: (optional) Currently unused
    """

    def __init__(
        self,
        credentials_fn: "Callable[[], CredentialTypes]",
        gspread_delay: float = 1.1,
        reauth_interval: int = 45,
        loop: asyncio.AbstractEventLoop = None,
        cell_flush_delay: float = 5.0,
    ):
        self.credentials_fn = credentials_fn
        self._loop = loop

        # seconds
        self.gspread_delay = gspread_delay
        self.cell_flush_delay = cell_flush_delay
        # arg is minutes, the stored value is seconds
        self.reauth_interval = reauth_interval * 60

        self._agc_cache: "Dict[float, AsyncioGspreadClient]" = {}
        self.auth_time: Optional[float] = None
        self.auth_lock = asyncio.Lock()
        self.last_call: Optional[float] = None
        self.call_lock = asyncio.Lock()

        self._cell_flusher_active = False

    async def _call(self, method, *args, **kwargs):
        if "api_call_count" in kwargs:
            api_call_count = kwargs["api_call_count"]
            del kwargs["api_call_count"]
        else:
            api_call_count = 1

        while True:
            await self.call_lock.acquire()
            try:
                fn = functools.partial(method, *args, **kwargs)
                for _ in range(api_call_count):
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
                if 400 <= code <= 499 and code != 429:
                    raise
                await self.handle_gspread_error(e, method, args, kwargs)
            except requests.RequestException as e:
                await self.handle_requests_error(e, method, args, kwargs)
            finally:
                self.call_lock.release()

    async def before_gspread_call(self, method, args, kwargs):
        """Called before invoking a :mod:`gspread` method. Optionally
        subclass this to implement custom logging, tracing, or modification
        of the method arguments.

        The default implementation logs the method name, args and kwargs.

        :param method: gspread class method to be invoked
        :param args: positional arguments for the gspread class method
        :param kwargs: keyword arguments for the gspread class method
        """
        logging.debug(
            "Calling {0} {1} {2}".format(method.__name__, str(args), str(kwargs))
        )

    async def handle_gspread_error(self, e, method, args, kwargs):
        """Called in the exception handler for a
        :class:`gspread.exceptions.APIError`. Optionally subclass this to
        implement custom error handling, error logging, rate limiting,
        backoff, or jitter.

        The default implementation logs the error and sleeps for
        :attr:`gspread_delay` seconds. It does not throw an exception
        of its own so it keeps retrying failed requests forever.

        gspread throws an :class:`~gspread.exceptions.APIError` when an error is
        returned from the Google API. `Google has some documentation on their
        HTTP status codes <https://cloud.google.com/apis/design/errors>`_.
        gspread makes a :class:`requests.Response` object accessible at
        :attr:`e.response`.

        Note that the internal :meth:`_call` method which invokes this method
        will not do so for any HTTP 400 statuses. These are errors that arise
        from mistaken usage of the Google API and are fatal. The exception is
        status code 429, the rate limiting status, to let this code handle
        client-side rate limiting.

        :param e: Exception object thrown by gspread
        :type e: :class:`~gspread.exceptions.APIError`
        :param method: gspread class method called
        :param args: positional arguments for the gspread class method
        :param kwargs: keyword arguments for the gspread class method
        """
        # By default, retry forever because sometimes Google just poops out and gives us a 500.
        # Subclass this to get custom error handling, backoff, jitter,
        # maybe even some cancellation
        logging.error(
            "Error while calling {0} {1} {2}. Sleeping for {3} seconds.".format(
                method.__name__, str(args), str(kwargs), self.gspread_delay
            )
        )
        # Wait a little bit just to keep from pounding Google
        await asyncio.sleep(self.gspread_delay)

    async def handle_requests_error(self, e, method, args, kwargs):
        """Called in the exception handler for a
        :class:`requests.RequestException`. Optionally subclass to implement
        custom error handling, error logging, rate limiting, backoff, or jitter.

        The default implementation logs the error and sleeps for
        :attr:`gspread_delay` seconds. It does not throw an exception of its own
        so it keeps retrying failed requests forever.

        gspread throws a :class:`~requests.RequestException` when a socket layer
        error occurs.

        :param e: Exception object thrown by gspread
        :type e: :class:`~requests.RequestException`
        :param method: gspread class method called
        :param args: positional arguments for the gspread class method
        :param kwargs: keyword arguments for the gspread class method
        """
        # By default, retry forever.
        logging.error(
            "Error while calling {0} {1} {2}. Sleeping for {3} seconds.".format(
                method.__name__, str(args), str(kwargs), self.gspread_delay
            )
        )
        # Wait a little bit just to keep from pounding Google
        await asyncio.sleep(self.gspread_delay)

    async def delay(self):
        """Called before invoking a :mod:`gspread` class method. Optionally
        subclass this to implement custom rate limiting.

        The default implementation figures out the delta between the last Google
        API call and now and sleeps for the delta if it is less than
        :attr:`gspread_delay`.
        """
        # Subclass this to customize rate limiting
        now = self._loop.time()
        if self.last_call is None:
            self.last_call = now
            return
        delta = now - self.last_call
        if delta >= self.gspread_delay:
            self.last_call = now
            return
        await asyncio.sleep(self.gspread_delay - delta)
        self.last_call = self._loop.time()
        return

    async def authorize(self) -> "AsyncioGspreadClient":
        """(Re)-authenticates an
        :class:`~gspread_asyncio.AsyncioGspreadClientManager`. You **must**
        call this method first to log in to the Google Spreadsheets API.

        Feel free to call this method often, even in a loop, as it caches
        Google's credentials and only re-authenticates when the credentials are
        nearing expiration.

        :returns: a ready-to-use :class:`~gspread_asyncio.AsyncioGspreadClient`
        """
        if self._loop is None:
            self._loop = asyncio.get_running_loop()

        await self.auth_lock.acquire()
        try:
            return await self._authorize()
        finally:
            self.auth_lock.release()

    async def _authorize(self):
        now = self._loop.time()
        if self.auth_time is None or self.auth_time + self.reauth_interval < now:
            creds = await self._loop.run_in_executor(None, self.credentials_fn)
            gc = await self._loop.run_in_executor(None, gspread.authorize, creds)
            agc = AsyncioGspreadClient(self, gc)
            self._agc_cache[now] = agc
            if self.auth_time is not None and self.auth_time in self._agc_cache:
                del self._agc_cache[self.auth_time]
            self.auth_time = now
        else:
            agc = self._agc_cache[self.auth_time]
        return agc


class AsyncioGspreadClient(object):
    """An :mod:`asyncio` wrapper for :class:`gspread.Client`. You **must**
    obtain instances of this class from
    :meth:`gspread_asyncio.AsyncioGspreadClientManager.authorize`.
    """

    def __init__(self, agcm: AsyncioGspreadClientManager, gc: gspread.Client):
        self.agcm = agcm
        self.gc = gc
        self._ss_cache_title: "Dict[str, AsyncioGspreadSpreadsheet]" = {}
        self._ss_cache_key: "Dict[str, AsyncioGspreadSpreadsheet]" = {}

    def _wrap_ss(self, ss: gspread.Spreadsheet) -> "AsyncioGspreadSpreadsheet":
        ass = AsyncioGspreadSpreadsheet(self.agcm, ss)
        self._ss_cache_title[ss.title] = ass
        self._ss_cache_key[ss.id] = ass
        return ass

    @_nowait
    async def copy(
        self,
        file_id: str,
        title: str = None,
        copy_permissions: bool = False,
        folder_id: str = None,
        copy_comments: bool = True,
    ) -> "AsyncioGspreadSpreadsheet":
        """Copies a spreadsheet.

        :param str file_id: A key of a spreadsheet to copy.
        :param str title: (optional) A title for the new spreadsheet.
        :param bool copy_permissions: (optional) If True, copy permissions from
            the original spreadsheet to the new spreadsheet.
        :param str folder_id: Id of the folder where we want to save
            the spreadsheet.
        :param bool copy_comments: (optional) If True, copy the comments from
            the original spreadsheet to the new spreadsheet.
        :param bool nowait: (optional) If true, return a scheduled future instead
            of waiting for the API call to complete.

        :returns: a :class:`~gspread_asyncio.AsyncioGspreadSpreadsheet` instance.

        .. versionadded:: 1.6
        .. note::
            If you're using custom credentials without the Drive scope, you need to add
            ``https://www.googleapis.com/auth/drive`` to your OAuth scope in order to use
            this method.
            Example::

                scope = [
                    'https://www.googleapis.com/auth/spreadsheets',
                    'https://www.googleapis.com/auth/drive'
                ]

            Otherwise, you will get an ``Insufficient Permission`` error
            when you try to copy a spreadsheet."""
        ss = await self.agcm._call(
            self.gc.copy,
            file_id,
            title=title,
            copy_permissions=copy_permissions,
            folder_id=folder_id,
            copy_comments=copy_comments,
        )
        ass = self._wrap_ss(ss)
        return ass

    async def create(
        self, title: str, folder_id: Optional[str] = None
    ) -> "AsyncioGspreadSpreadsheet":
        """Create a new Google Spreadsheet. Wraps
        :meth:`gspread.Client.create`.

        :param str title: Human-readable name of the new spreadsheet.
        :param str folder_id: Id of the folder where we want to save the spreadsheet.
        :rtype: :class:`gspread_asyncio.AsyncioGspreadSpreadsheet`
        """
        ss = await self.agcm._call(self.gc.create, title, folder_id)
        ass = self._wrap_ss(ss)
        return ass

    @_nowait
    async def del_spreadsheet(self, file_id: str):
        """Delete a Google Spreadsheet. Wraps
        :meth:`gspread.Client.del_spreadsheet`.

        :param file_id: Google's spreadsheet id
        :type file_id: str
        :param bool nowait: (optional) If true, return a scheduled future
            instead of waiting for the API call to complete.
        """
        if file_id in self._ss_cache_key:
            del self._ss_cache_key[file_id]

        titles = [
            title for title, ss in self._ss_cache_title.items() if ss.id == file_id
        ]
        for title in titles:
            del self._ss_cache_title[title]

        return await self.agcm._call(self.gc.del_spreadsheet, file_id)

    async def export(
        self,
        file_id: str,
        format: gspread.utils.ExportFormat = gspread.utils.ExportFormat.PDF,
    ) -> bytes:
        """Export the spreadsheet in the format. Wraps :meth:`gspread.Client.export`.

        :param str file_id: A key of a spreadsheet to export
        :param str format: The format of the resulting file.
            Possible values are:

                * :data:`gspread.utils.ExportFormat.PDF`
                * :data:`gspread.utils.ExportFormat.EXCEL`
                * :data:`gspread.utils.ExportFormat.CSV`
                * :data:`gspread.utils.ExportFormat.OPEN_OFFICE_SHEET`
                * :data:`gspread.utils.ExportFormat.TSV`
                * :data:`gspread.utils.ExportFormat.ZIPPED_HTML`

            See `ExportFormat`_ in the Drive API.
        :type format: :data:`gspread.utils.ExportFormat`

        :returns: The content of the exported file.
        :rtype: bytes

        .. _ExportFormat: https://developers.google.com/drive/api/guides/ref-export-formats

        .. versionadded:: 1.6
        """
        return await self.agcm._call(
            self.gc.export,
            file_id,
            format=format,
        )

    @_nowait
    async def import_csv(self, file_id: str, data: str):
        """Upload a csv file and save its data into the first page of the
        Google Spreadsheet. Wraps :meth:`gspread.Client.import_csv`.

        :param str file_id: Google's spreadsheet id
        :param str data: The CSV file
        :param bool nowait: (optional) If true, return a scheduled future instead
            of waiting for the API call to complete.
        """
        return await self.agcm._call(self.gc.import_csv, file_id, data)

    @_nowait
    async def insert_permission(
        self,
        file_id: str,
        value: Optional[str],
        perm_type: str,
        role: str,
        notify: bool = True,
        email_message: str = None,
        with_link: bool = False,
    ):
        """Add new permission to a Google Spreadsheet. Wraps
        :meth:`gspread.Client.insert_permission`.

        :param str file_id: Google's spreadsheet id
        :param value: user or group e-mail address, domain name or None for
            ‘default’ type.
        :type value: str, None
        :param str perm_type: Allowed values are:
            ``user``, ``group``, ``domain``, ``anyone``.
        :param str role: the primary role for this user. Allowed values are:
            ``owner``, ``writer``, ``reader``.
        :param bool notify: (optional) Whether to send an email to the target
            user/domain.
        :param str email_message: (optional) The email to be sent if notify=True
        :param bool with_link: (optional) Whether the link is required for this
            permission to be active.
        :param bool nowait: (optional) If true, return a scheduled future instead of
            waiting for the API call to complete.
        """
        return await self.agcm._call(
            self.gc.insert_permission,
            file_id,
            value,
            perm_type,
            role,
            notify=notify,
            email_message=email_message,
            with_link=with_link,
        )

    async def list_permissions(self, file_id: str):
        """List the permissions of a Google Spreadsheet. Wraps
        :meth:`gspread.Client.list_permissions`.

        :param str file_id: Google's spreadsheet id

        :returns: Some kind of object with permissions in it. I don't know,
            the author of gspread forgot to document it.
        """
        return await self.agcm._call(self.gc.list_permissions, file_id)

    # N.B. list_spreadsheet_files does multiple calls, would require monkeypatching
    # to implement here.

    async def login(self):
        raise NotImplemented(
            "Use AsyncioGspreadClientManager.authorize()" "to create a gspread client"
        )

    async def open(self, title: str) -> "AsyncioGspreadSpreadsheet":
        """Opens a Google Spreadsheet by title. Wraps
        :meth:`gspread.Client.open`.

        Feel free to call this method often, even in a loop, as it caches the
        underlying spreadsheet object.

        :param str title: The title of the spreadsheet
        :rtype: :class:`~gspread_asyncio.AsyncioGspreadSpreadsheet`
        """
        if title in self._ss_cache_title:
            return self._ss_cache_title[title]
        ss = await self.agcm._call(self.gc.open, title)
        ass = self._wrap_ss(ss)
        return ass

    async def open_by_key(self, key: str) -> "AsyncioGspreadSpreadsheet":
        """Opens a Google Spreadsheet by spreasheet id. Wraps
        :meth:`gspread.Client.open_by_key`.

        Feel free to call this method often, even in a loop, as it caches
        the underlying spreadsheet object.

        :param str key: Google's spreadsheet id
        :rtype: :class:`~gspread_asyncio.AsyncioGspreadSpreadsheet`
        """
        if key in self._ss_cache_key:
            return self._ss_cache_key[key]
        ss = await self.agcm._call(self.gc.open_by_key, key)
        ass = self._wrap_ss(ss)
        return ass

    async def open_by_url(self, url: str) -> "AsyncioGspreadSpreadsheet":
        """Opens a Google Spreadsheet from a URL. Wraps
        :meth:`gspread.Client.open_by_url`.

        Feel free to call this method often, even in a loop, as it caches the
        underlying spreadsheet object.

        :param str url: URL to a Google Spreadsheet
        :rtype: :class:`~gspread_asyncio.AsyncioGspreadSpreadsheet`
        """
        ss_id = extract_id_from_url(url)
        return await self.open_by_key(ss_id)

    async def openall(
        self, title: Optional[str] = None
    ) -> "List[AsyncioGspreadSpreadsheet]":
        """Open all available spreadsheets. Wraps
        :meth:`gspread.Client.openall`.

        Feel free to call this method often, even in a loop, as it caches
        the underlying spreadsheet objects.

        :param str title: (optional) If specified can be used to filter spreadsheets
            by title.
        :rtype: :py:class:`~typing.List`\\[:class:`~gspread_asyncio.AsyncioGspreadSpreadsheet`\\]
        """
        sses = await self.agcm._call(self.gc.openall, title=title)
        asses = []
        for ss in sses:
            ass = self._wrap_ss(ss)
            asses.append(ass)
        return asses

    @_nowait
    async def remove_permission(self, file_id: str, permission_id: str):
        """Delete permissions from a Google Spreadsheet. Wraps
        :meth:`gspread.Client.remove_permission`.

        :param str file_id: Google's spreadsheet id
        :param str permission_id: The permission's id
        :param bool nowait: (optional) If true, return a scheduled future instead
            of waiting for the API call to complete.
        """
        return await self.agcm._call(self.gc.remove_permission, file_id, permission_id)


class AsyncioGspreadSpreadsheet(object):
    """An :mod:`asyncio` wrapper for :class:`gspread.Spreadsheet`.
    You **must** obtain instances of this class from
    :meth:`AsyncioGspreadClient.open`,
    :meth:`AsyncioGspreadClient.open_by_key`,
    :meth:`AsyncioGspreadClient.open_by_url`,
    or :meth:`AsyncioGspreadClient.openall`.
    """

    def __init__(self, agcm, ss: gspread.Spreadsheet):
        self.agcm = agcm
        self.ss = ss

        self._ws_cache_title: "Dict[str, AsyncioGspreadWorksheet]" = {}
        self._ws_cache_idx: "Dict[int, AsyncioGspreadWorksheet]" = {}

    def __repr__(self):
        return f"<{self.__class__.__name__} id:{self.ss.id}>"

    def _wrap_ws(self, ws: gspread.Worksheet) -> "AsyncioGspreadWorksheet":
        aws = AsyncioGspreadWorksheet(self.agcm, ws)
        self._ws_cache_title[aws.ws.title] = aws
        self._ws_cache_idx[aws.ws._properties["index"]] = aws
        return aws

    async def add_worksheet(
        self, title: str, rows: int, cols: int, index: Optional[int] = None
    ) -> "AsyncioGspreadWorksheet":
        """Add new worksheet (tab) to a spreadsheet. Wraps
        :meth:`gspread.Spreadsheet.add_worksheet`.

        :param str title: Human-readable title for the new worksheet
        :param int rows: Number of rows for the new worksheet
        :param int cols: Number of columns for the new worksheet
        :param int index: (optional) Position of the sheet
        :rtype: :py:class:`~gspread_asyncio.AsyncioGspreadWorksheet`
        """
        ws = await self.agcm._call(
            self.ss.add_worksheet, title, rows, cols, index=index
        )
        aws = self._wrap_ws(ws)
        return aws

    @_nowait
    async def batch_update(self, body: dict) -> dict:
        """Lower-level method that directly calls `spreadsheets/<ID>:batchUpdate <https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets/batchUpdate>`_.

        :param dict body: `Request body <https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets/batchUpdate#request-body>`_.
        :param bool nowait: (optional) If true, return a scheduled future instead
            of waiting for the API call to complete.

        :returns: `Response body <https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets/batchUpdate#response-body>`_.
        :rtype: dict

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ss.batch_update, body)

    # creationTime / lastUpdateTime are not implemented
    # as they do i/o under the hood, with list_spreadsheet_files

    @_nowait
    async def del_worksheet(self, worksheet: "AsyncioGspreadWorksheet"):
        """Delete a worksheet (tab) from a spreadsheet. Wraps
        :meth:`gspread.Spreadsheet.del_worksheet`.

        :param worksheet: Worksheet to delete
        :type worksheet: :class:`gspread.Worksheet`
        :param bool nowait: (optional) If true, return a scheduled future instead
            of waiting for the API call to complete.
        """
        if worksheet.ws.title in self._ws_cache_title:
            del self._ws_cache_title[worksheet.ws.title]

        ws_idx = worksheet.ws._properties["index"]
        if ws_idx in self._ws_cache_idx:
            del self._ws_cache_idx[ws_idx]

        return await self.agcm._call(self.ss.del_worksheet, worksheet.ws)

    async def duplicate_sheet(
        self,
        source_sheet_id: int,
        insert_sheet_index: int = None,
        new_sheet_id: int = None,
        new_sheet_name: int = None,
    ) -> "AsyncioGspreadWorksheet":
        """Duplicates the contents of a sheet. Wraps :meth:`gspread.Worksheet.duplicate_sheet`.

        :param int source_sheet_id: The sheet ID to duplicate.
        :param int insert_sheet_index: (optional) The zero-based index
            where the new sheet should be inserted.
            The index of all sheets after this are
            incremented.
        :param int new_sheet_id: (optional) The ID of the new sheet.
            If not set, an ID is chosen. If set, the ID
            must not conflict with any existing sheet ID.
            If set, it must be non-negative.
        :param str new_sheet_name: (optional) The name of the new sheet.
            If empty, a new name is chosen for you.
        :returns: a newly created :class:`AsyncioGspreadWorksheet`

        .. versionadded:: 1.6
        """
        ws = await self.agcm._call(
            self.ss.duplicate_sheet,
            source_sheet_id,
            insert_sheet_index=insert_sheet_index,
            new_sheet_id=new_sheet_id,
            new_sheet_name=new_sheet_name,
        )
        aws = self._wrap_ws(ws)
        return aws

    async def export(self, format=gspread.utils.ExportFormat.PDF) -> bytes:
        """Export the spreadsheet in the format. Wraps :meth:`gspread.Spreadsheet.export`.

        :param str format: The format of the resulting file.
            Possible values are:

                * :data:`gspread.utils.ExportFormat.PDF`
                * :data:`gspread.utils.ExportFormat.EXCEL`
                * :data:`gspread.utils.ExportFormat.CSV`
                * :data:`gspread.utils.ExportFormat.OPEN_OFFICE_SHEET`
                * :data:`gspread.utils.ExportFormat.TSV`
                * :data:`gspread.utils.ExportFormat.ZIPPED_HTML`

            See `ExportFormat`_ in the Drive API.
        :type format: :data:`gspread.utils.ExportFormat`

        :returns: The content of the exported file.
        :rtype: bytes

        .. _ExportFormat: https://developers.google.com/drive/api/guides/ref-export-formats

        .. versionadded:: 1.6
        """
        return await self.agcm._call(
            self.ss.export,
            format=format,
        )

    async def fetch_sheet_metadata(self, params: dict = None) -> dict:
        """Retrieve spreadsheet metadata.

        :param dict params: (optional) `Query parameters`_.

        :returns: `Response body`_.
        :rtype: dict

        .. versionadded:: 1.8.1
        """
        return await self.agcm._call(self.ss.fetch_sheet_metadata, params=params)

    async def get_worksheet(self, index: int) -> "AsyncioGspreadWorksheet":
        """Retrieves a worksheet (tab) from a spreadsheet by index number.
        Indexes start from zero. Wraps
        :meth:`gspread.Spreadsheet.get_worksheet`.

        Feel free to call this method often, even in a loop, as it caches the
        underlying worksheet object.

        :param int index: Index of worksheet
        :rtype: :class:`AsyncioGspreadWorksheet`
        """
        if index in self._ws_cache_idx:
            return self._ws_cache_idx[index]
        ws = await self.agcm._call(self.ss.get_worksheet, index)
        aws = self._wrap_ws(ws)
        return aws

    async def get_worksheet_by_id(self, id: int) -> "AsyncioGspreadWorksheet":
        """Returns a worksheet with specified `worksheet id`.

        :param int id: The id of a worksheet. it can be seen in the url as the value of the parameter 'gid'.

        :rtype: an instance of :class:`AsyncioGspreadWorksheet`.

        :raises: :class:`gspread.exceptions.WorksheetNotFound`: if can't find the worksheet

        .. versionadded:: 1.5
        """
        ws = await self.agcm._call(self.ss.get_worksheet_by_id, id)
        aws = self._wrap_ws(ws)
        return aws

    @property
    def id(self) -> str:
        """:returns: Google's spreadsheet id.

        :rtype: str
        """
        return self.ss.id

    async def list_named_ranges(self) -> list:
        """Lists the spreadsheet's named ranges.
        Wraps :meth:`gspread.Spreadsheet.list_named_ranges`.

        :returns: The gspread author forgot to document this

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ss.list_named_ranges)

    async def list_permissions(self) -> list:
        """List the permissions of a Google Spreadsheet.
        Wraps :meth:`gspread.Spreadsheet.list_permissions`.

        :returns: The gspread author forgot to document this
        """
        return await self.agcm._call(self.ss.list_permissions)

    async def list_protected_ranges(self) -> list:
        """Lists the spreadsheet’s protected named ranges.
        Wraps :meth:`gspread.Spreadsheet.list_protected_ranges`.

        :returns: The gspread author forgot to document this

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ss.list_protected_ranges)

    @property
    def locale(self) -> str:
        """Spreadsheet locale. Wraps :meth:`gspread.Spreadsheet.locale`.

        :rtype: :py:class:`str`

        .. versionadded:: 1.6
        """
        return self.ss.locale

    async def named_range(self, named_range: str) -> List[gspread.cell.Cell]:
        """return a list of :class:`gspread.cell.Cell` objects from
        the specified named range.

        :param named_range: A string with a named range value to fetch.
        :type named_range: str

        :rtype: :class:`~typing.List`\\[:class:`gspread.Cell`\\]

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ss.named_range, named_range)

    @_nowait
    async def remove_permissions(self, value: str, role: str = "any"):
        """Remove permissions from a user or domain. Wraps
        :meth:`gspread.Spreadsheet.remove_permissions`.

        :param str value: User or domain to remove permissions from
        :param str role: (optional) Permission to remove. Defaults to all
            permissions.
        :param bool nowait: (optional) If true, return a scheduled future
            instead of waiting for the API call to complete.
        """
        return await self.agcm._call(self.ss.remove_permissions, value, role=role)

    @_nowait
    async def transfer_ownership(self, permission_id: str):
        """Transfer the ownership of this file to a new user.

        It is necessary to first create the permission with the new owner's email address,
        get the permission ID then use this method to transfer the ownership.

        .. note::

           You can list all permission using :meth:`gspread.spreadsheet.Spreadsheet.list_permissions`

        .. warning::

           You can only transfer ownership to a new user, you cannot transfer ownership to a group
           or a domain email address.

        :param str permission_id: New permission ID
        :param bool nowait: (optional) If true, return a scheduled future
            instead of waiting for the API call to complete.

        .. versionadded:: 1.7
        """
        return await self.agcm._call(self.ss.transfer_ownership, permission_id)

    @_nowait
    async def accept_ownership(self, permission_id: str):
        """Accept the pending ownership request on that file.

        It is necessary to edit the permission with the pending ownership.

        .. note::
           You can only accept ownership transfer for the user currently being used.

        .. versionadded:: 1.7

        :param str permission_id: New permission ID
        :param bool nowait: (optional) If true, return a scheduled future
            instead of waiting for the API call to complete.
        """
        return await self.agcm._call(self.ss.accept_ownership, permission_id)

    @_nowait
    async def reorder_worksheets(
        self, worksheets_in_desired_order: "Iterable[AsyncioGspreadWorksheet]"
    ):
        """Updates the ``index`` property of each Worksheet to reflect
        its index in the provided sequence of Worksheets. Wraps :meth:`gspread.worksheet.Worksheet.reorder_worksheet`.

        :param worksheets_in_desired_order: Iterable of Worksheet objects in desired order.
        :type worksheets_in_desired_order: :class:`~typing.Iterable`\\[:class:`AsyncioGspreadSpreadsheet`\\]
        :param bool nowait: (optional) If true, return a scheduled future instead of
            waiting for the API call to complete.

        Note: If you omit some of the Spreadsheet's existing Worksheet objects from
        the provided sequence, those Worksheets will be appended to the end of the sequence
        in the order that they appear in the list returned by :meth:`gspread.spreadsheet.Spreadsheet.worksheets`.

        .. versionadded:: 1.6
        """
        return await self.agcm._call(
            self.ss.reorder_worksheets, (aws.ws for aws in worksheets_in_desired_order)
        )

    async def get_sheet1(self) -> "AsyncioGspreadWorksheet":
        """:returns: Shortcut for getting the first worksheet.
        :rtype: :class:`AsyncioGspreadWorksheet`
        """
        return await self.get_worksheet(0)

    @property
    def timezone(self) -> str:
        """:returns: Title of the spreadsheet.
        :rtype: str

        .. versionadded:: 1.6
        """
        return self.ss.timezone

    @property
    def title(self) -> str:
        """:returns: Title of the spreadsheet.
        :rtype: str
        """
        return self.ss.title

    @_nowait
    async def update_locale(self, locale: str):
        """Update the locale of the spreadsheet.

        Can be any of the ISO 639-1 language codes, such as: de, fr, en, ...
        Or an ISO 639-2 if no ISO 639-1 exists.
        Or a combination of the ISO language code and country code,
        such as en_US, de_CH, fr_FR, ...

        :param str locale: New locale
        :param bool nowait: (optional) If true, return a scheduled future instead of
            waiting for the API call to complete.

        .. note::
            Note: when updating this field, not all locales/languages are supported.

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ss.update_locale, locale)

    @_nowait
    async def update_timezone(self, timezone: str):
        """Updates the current spreadsheet timezone.
        Can be any timezone in CLDR format such as "America/New_York"
        or a custom time zone such as GMT-07:00.

        :param str timezone: New timezone
        :param bool nowait: (optional) If true, return a scheduled future instead of
            waiting for the API call to complete.

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ss.update_timezone, timezone)

    async def update_title(self, title: str):
        """Renames the spreadsheet.

        :param str title: A new title.
        :param bool nowait: (optional) If true, return a scheduled future instead of
            waiting for the API call to complete.

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ss.update_title, title)

    @property
    def url(self) -> str:
        return self.ss.url

    async def values_append(self, range: str, params: dict, body: dict) -> dict:
        """Lower-level method that directly calls `spreadsheets/<ID>/values:append <https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets.values/append>`_.

        :param str range: The `A1 notation <https://developers.google.com/sheets/api/guides/concepts#a1_notation>`_
                          of a range to search for a logical table of data. Values will be appended after the last row of the table.
        :param dict params: `Query parameters <https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets.values/append#query-parameters>`_.
        :param dict body: `Request body`_.
        :returns: `Response body`_.
        :rtype: dict

        .. versionadded:: 1.6
        """
        return await self.agcm._call(
            self.ss.values_append,
            range,
            params,
            body,
        )

    async def values_batch_get(self, ranges: List[str], params: dict = None) -> dict:
        """Lower-level method that directly calls `spreadsheets/<ID>/values:batchGet <https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets.values/batchGet>`_.

        :param ranges: List of ranges in the `A1 notation <https://developers.google.com/sheets/api/guides/concepts#a1_notation>`_ of the values to retrieve.
        :param dict params: (optional) `Query parameters`_.
        :returns: `Response body`_.
        :rtype: dict

        .. versionadded:: 1.6
        """
        return await self.agcm._call(
            self.ss.values_batch_get,
            ranges,
            params=params,
        )

    async def values_clear(self, range: str) -> dict:
        """Lower-level method that directly calls `spreadsheets/<ID>/values:clear <https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets.values/clear>`_.

        :param str range: The `A1 notation <https://developers.google.com/sheets/api/guides/concepts#a1_notation>`_ of the values to clear.

        :returns: `Response body`_.
        :rtype: dict

        .. versionadded:: 1.6
        """
        return await self.agcm._call(
            self.ss.values_clear,
            range,
        )

    async def values_get(self, range: str, params: dict = None) -> dict:
        """Lower-level method that directly calls `spreadsheets/<ID>/values/<range> <https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets.values/get>`_.

        :param str range: The `A1 notation <https://developers.google.com/sheets/api/guides/concepts#a1_notation>`_ of the values to retrieve.
        :param dict params: (optional) `Query parameters`_.

        :returns: `Response body`_.
        :rtype: dict

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ss.values_get, range, params=params)

    async def values_update(
        self, range: str, params: dict = None, body: dict = None
    ) -> dict:
        """Lower-level method that directly calls `spreadsheets/<ID>/values/<range>`_.

        :param str range: The `A1 notation <https://developers.google.com/sheets/api/guides/concepts#a1_notation>`_ of the values to update.
        :param dict params: (optional) `Query parameters`_.
        :param dict body: (optional) `Request body`_.

        :returns: `Response body`_.
        :rtype: dict

        Example::

            sh.values_update(
                'Sheet1!A2',
                params={
                    'valueInputOption': 'USER_ENTERED'
                },
                body={
                    'values': [[1, 2, 3]]
                }
            )

        .. versionadded:: 1.6
        """
        return await self.agcm._call(
            self.ss.values_update,
            range,
            params=params,
            body=body,
        )

    async def worksheet(self, title: str) -> "AsyncioGspreadWorksheet":
        """Gets a worksheet (tab) by title. Wraps
        :meth:`gspread.Spreadsheet.worksheet`.

        Feel free to call this method often, even in a loop, as it caches
        the underlying worksheet object.

        :param str title: Human-readable title of the worksheet.
        :rtype: :class:`~gspread_asyncio.AsyncioGspreadWorksheet`
        """
        if title in self._ws_cache_title:
            return self._ws_cache_title[title]
        ws = await self.agcm._call(self.ss.worksheet, title)
        aws = self._wrap_ws(ws)
        return aws

    async def worksheets(self) -> "List[AsyncioGspreadWorksheet]":
        """Gets all worksheets (tabs) in a spreadsheet.
        Wraps :meth:`gspread.Spreadsheet.worksheets`.

        Feel free to call this method often, even in a loop, as it caches
        the underlying worksheet objects.

        :rtype: :py:class:`~typing.List`\\[:class:`~gspread_asyncio.AsyncioGspreadWorksheet`\\]
        """
        wses = await self.agcm._call(self.ss.worksheets)
        awses = []
        for ws in wses:
            aws = self._wrap_ws(ws)
            awses.append(aws)
        return awses


class AsyncioGspreadWorksheet(object):
    """An :mod:`asyncio` wrapper for :class:`gspread.Worksheet`.
    You **must** obtain instances of this class from
    :meth:`AsyncioGspreadSpreadsheet.add_worksheet`,
    :meth:`AsyncioGspreadSpreadsheet.get_worksheet`,
    :meth:`AsyncioGspreadSpreadsheet.worksheet`,
    or :meth:`AsyncioGspreadSpreadsheet.worksheets`.
    """

    def __init__(self, agcm, ws: gspread.Worksheet):
        self.agcm = agcm
        self.ws = ws

    def __repr__(self):
        return f"<{self.__class__.__name__} id:{self.ws.id}>"

    async def acell(
        self,
        label: str,
        value_render_option: gspread.utils.ValueRenderOption = gspread.utils.ValueRenderOption.formatted,
    ) -> gspread.Cell:
        """Get cell by label (A1 notation).
        Wraps :meth:`gspread.Worksheet.acell`.

        :param label: Cell label in A1 notation
            Letter case is ignored.
        :type label: str
        :param value_render_option: (optional) Determines how values should be
            rendered in the output. See
            `ValueRenderOption`_ in the Sheets API.
        :type value_render_option: :py:obj:`gspread.utils.ValueRenderOption`
        :rtype: :class:`gspread.Cell`
        """
        return await self.agcm._call(
            self.ws.acell, label, value_render_option=value_render_option
        )

    @_nowait
    async def add_cols(self, cols: int):
        """Adds columns to worksheet. Wraps
        :meth:`gspread.Worksheet.add_cols`.

        :param int cols: Number of new columns to add.
        :param bool nowait: (optional) If true, return a scheduled future
            instead of waiting for the API call to complete.
        """
        return await self.agcm._call(self.ws.add_cols, cols)

    async def add_dimension_group_columns(self, start: int, end: int):
        """
        Group columns in order to hide them in the UI.

        .. note::

            API behavior with nested groups and non-matching ``[start:end)``
            range can be found here: `Add Dimension Group Request`_

            .. _Add Dimension Group Request: https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets/request#AddDimensionGroupRequest

        :param int start: The start (inclusive) of the group
        :param int end: The end (exclusive) of the group

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ws.add_dimension_group_columns, start, end)

    async def add_dimension_group_rows(self, start: int, end: int):
        """
        Group rows in order to hide them in the UI.

        .. note::

            API behavior with nested groups and non-matching ``[start:end)``
            range can be found here: `Add Dimension Group Request`_

            .. _Add Dimension Group Request: https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets/request#AddDimensionGroupRequest

        :param int start: The start (inclusive) of the group
        :param int end: The end (exclusive) of the group

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ws.add_dimension_group_rows, start, end)

    @_nowait
    async def add_protected_range(
        self,
        name: str,
        editor_users_emails: List[str] = None,
        editor_groups_emails: List[str] = None,
        description: str = None,
        warning_only: bool = False,
        requesting_user_can_edit: bool = False,
    ):
        """Add protected range to the sheet. Only the editors can edit
        the protected range.

        :param str name: A string with range value in A1 notation,
            e.g. 'A1:A5'.

        :param list editor_users_emails: (optional) The email addresses of
            users with edit access to the protected range.
        :param list editor_groups_emails: (optional) The email addresses of
            groups with edit access to the protected range.
        :param str description: (optional) Description for the protected
            ranges.
        :param bool warning_only: (optional) When true this protected range
            will show a warning when editing. Defaults to ``False``.
        :param bool requesting_user_can_edit: (optional) True if the user
            who requested this protected range can edit the protected cells.
            Defaults to ``False``.
        :param bool nowait: (optional) If true, return a scheduled future instead of
            waiting for the API call to complete.

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
            api_call_count=2,
        )

    @_nowait
    async def add_rows(self, rows: int):
        """Adds rows to worksheet. Wraps
        :py:meth:`gspread.worksheet.Worksheet.add_rows`.

        :param int rows: Number of new rows to add.
        :param bool nowait: (optional) If true, return a scheduled future instead
            of waiting for the API call to complete.
        """
        return await self.agcm._call(self.ws.add_rows, rows)

    @_nowait
    async def append_row(
        self,
        values: List[str],
        value_input_option: gspread.utils.ValueInputOption = gspread.utils.ValueInputOption.raw,
        insert_data_option=None,
        table_range=None,
    ):
        """Adds a row to the worksheet and populates it with values.
        Widens the worksheet if there are more values than columns. Wraps
        :meth:`gspread.Worksheet.append_row`.

        :param values: List of values for the new row.
        :type values: :py:class:`~typing.List`\\[`str`\\]
        :param `gspread.utils.ValueInputOption` value_input_option:
            (optional) Determines how values should be
            rendered in the output. See
            `ValueInputOption`_ in the Sheets API.
        :param str insert_data_option: (optional) Determines how the input data
            should be inserted. See `InsertDataOption`_ in the Sheets API
            reference.
        :param str table_range: (optional) The A1 notation of a range to search
            for a logical table of data. Values are appended after the last row
            of the table. Examples: ``A1`` or ``B2:D4``
        :param bool nowait: (optional) If true, return a scheduled future instead of
            waiting for the API call to complete.

        .. _ValueInputOption: https://developers.google.com/sheets/api/reference/rest/v4/ValueInputOption
        """
        return await self.agcm._call(
            self.ws.append_row,
            values,
            value_input_option=value_input_option,
            insert_data_option=insert_data_option,
            table_range=table_range,
        )

    @_nowait
    async def append_rows(
        self,
        values: List[List[str]],
        value_input_option: gspread.utils.ValueInputOption = gspread.utils.ValueInputOption.raw,
        insert_data_option: str = None,
        table_range: str = None,
    ):
        """Adds multiple rows to the worksheet and populates them with values.

        Widens the worksheet if there are more values than columns.

        NOTE: it doesn't extend the filtered range.

        Wraps :meth:`gspread.Worksheet.append_rows`.

        :param list values: List of rows each row is List of values for
            the new row.
        :param `gspread.utils.ValueInputOption` value_input_option:
            (optional) Determines how input data
            should be interpreted. See `ValueInputOption`_ in the Sheets API.
        :param str insert_data_option: (optional) Determines how the input data
            should be inserted. See `InsertDataOption`_ in the Sheets API
            reference.
        :param str table_range: (optional) The A1 notation of a range to search
            for a logical table of data. Values are appended after the last row
            of the table. Examples: `A1` or `B2:D4`
        :param bool nowait: (optional) If true, return a scheduled future instead of
            waiting for the API call to complete.

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

    @_nowait
    async def batch_clear(self, ranges: List[str]):
        """Clears multiple ranges of cells with 1 API call.

        Wraps :meth:`gspread.Worksheet.batch_clear`.

        :param ranges: List of 'A1:B1' or named ranges to clear.
        :type ranges: :py:class:`~typing.List`\\[`str`\\]
        :param bool nowait: (optional) If true, return a scheduled future instead of
            waiting for the API call to complete.

        .. versionadded:: 1.5
        """
        return await self.agcm._call(self.ws.batch_clear, ranges)

    @_nowait
    async def batch_format(self, formats: List[dict]):
        """Formats cells in batch.

        :param list formats: List of ranges to format and the new format to apply
            to each range.

            The list is composed of dict objects with the following keys/values:

            * range : A1 range notation
            * format : a valid dict object with the format to apply
              for that range see `CellFormat`_ in the Sheets API for available fields.

        :param bool nowait: (optional) If true, return a scheduled future instead of
            waiting for the API call to complete.

        .. _CellFormat: https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets/cells#cellformat

        Examples::

            # Format the range ``A1:C1`` with bold text
            # and format the range ``A2:C2`` a font size of 16
            formats = [
                {
                    "range": "A1:C1",
                    "format": {
                        "textFormat": {
                            "bold": True,
                        },
                    },
                },
                {
                    "range": "A2:C2",
                    "format": {
                        "textFormat": {
                            "fontSize": 16,
                        },
                    },
                },
            ]
            worksheet.batch_update(formats)

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ws.batch_format, formats)

    async def batch_get(
        self,
        ranges: List[str],
        major_dimension: str = None,
        value_render_option: gspread.utils.ValueRenderOption = None,
        date_time_render_option: gspread.utils.DateTimeOption = None,
    ) -> list:
        """Returns one or more ranges of values from the sheet.

        :param list ranges: List of cell ranges in the A1 notation or named
            ranges.

        :param str major_dimension: (optional) The major dimension that results
            should use.

        :param `gspread.utils.ValueRenderOption` value_render_option: (optional) How values should be
            represented in the output. The default render option
            is `FORMATTED_VALUE`.

        :param `gspread.utils.DateTimeOption` date_time_render_option: (optional) How dates, times, and
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

    @_nowait
    async def batch_update(
        self,
        data,
        raw=True,
        value_input_option: gspread.utils.ValueInputOption = None,
        include_values_in_response=None,
        response_value_render_option: gspread.utils.ValueRenderOption = None,
        response_date_time_render_option: gspread.utils.DateTimeOption = None,
    ):
        """Sets values in one or more cell ranges of the sheet at
        once. Wraps :meth:`gspread.Worksheet.batch_update`.

        :param data: List of dictionaries in the form of
            `{'range': '...', 'values': [[.., ..], ...]}` where `range`
            is a target range to update in A1 notation or a named range,
            and `values` is a list of lists containing new values.

            For input, supported value types are: bool, string, and double.
            Null values will be skipped. To set a cell to an empty value, set
            the string value to an empty string.
        :type data: :py:class:`list`

        :param bool raw: (optional) Force value_input_option="RAW"

        :param `gspread.utils.ValueInputOption` value_input_option:
            (optional) How the input data should be
            interpreted. Possible values are:

            ``RAW``
                The values the user has entered will not be parsed
                and will be stored as-is.

            ``USER_ENTERED``
                The values will be parsed as if the user typed them
                into the UI. Numbers will stay as numbers, but
                strings may be converted to numbers, dates, etc.
                following the same rules that are applied when
                entering text into a cell via the Google Sheets UI.

        :param bool include_values_in_response: (optional) Determines if the
            update response should include the values of the cells that were
            updated. By default, responses do not include the updated values.

        :param `gspread.utils.ValueRenderOption` response_value_render_option:
            (optional) Determines how
            values in the response should be rendered.
            See `ValueRenderOption`_ in the Sheets API.

        :param `gspread.utils.DateTimeOption` response_date_time_render_option: (optional)
            Determines how dates, times, and durations in the response
            should be rendered. See `DateTimeRenderOption`_ in the Sheets API.

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
        .. _ValueRenderOption: https://developers.google.com/sheets/api/reference/rest/v4/ValueRenderOption
        .. _DateTimeRenderOption: https://developers.google.com/sheets/api/reference/rest/v4/DateTimeRenderOption
        """
        return await self.agcm._call(
            self.ws.batch_update,
            data,
            raw=raw,
            value_input_option=value_input_option,
            include_values_in_response=include_values_in_response,
            response_value_render_option=response_value_render_option,
            response_date_time_render_option=response_date_time_render_option,
        )

    async def cell(
        self,
        row: int,
        col: int,
        value_render_option: gspread.utils.ValueRenderOption = gspread.utils.ValueRenderOption.formatted,
    ) -> gspread.Cell:
        """Returns an instance of a :class:`gspread.Cell` located at
        `row` and `col` column. Wraps :meth:`gspread.Worksheet.cell`.

        :param int row: Row number.
        :param int col: Column number.
        :param `gspread.utils.ValueRenderOption` value_render_option:
            (optional) Determines how values should be
            rendered in the output. See
            `ValueRenderOption`_ in the Sheets API.
        :rtype: :class:`gspread.Cell`

        .. _ValueRenderOption: https://developers.google.com/sheets/api/reference/rest/v4/ValueRenderOption
        """
        return await self.agcm._call(
            self.ws.cell, row, col, value_render_option=value_render_option
        )

    @_nowait
    async def clear(self):
        """Clears all cells in the worksheet. Wraps
        :meth:`gspread.Worksheet.clear`.

        :param bool nowait: (optional) If true, return a scheduled future instead
            of waiting for the API call to complete.
        """
        return await self.agcm._call(self.ws.clear)

    @_nowait
    async def clear_basic_filter(self):
        """Remove the basic filter from a worksheet.

        :param bool nowait: (optional) If true, return a scheduled future instead
            of waiting for the API call to complete.

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ws.clear_basic_filter)

    @_nowait
    async def clear_note(self, cell: str):
        """Clear a note. The note is attached to a certain cell.

        :param str cell: A string with a cell coordinates in A1 notation,
            e.g. 'D7'.

        :param bool nowait: (optional) If true, return a scheduled future instead
            of waiting for the API call to complete.

        .. versionadded:: 1.4
        """

        return await self.agcm._call(self.ws.clear_note, cell)

    @property
    def col_count(self) -> int:
        """:returns: Number of columns in the worksheet.
        :rtype: int
        """
        return self.ws.col_count

    async def col_values(
        self,
        col,
        value_render_option: gspread.utils.ValueRenderOption = gspread.utils.ValueRenderOption.formatted,
    ) -> List[Optional[str]]:
        """Returns a list of all values in column `col`. Wraps
        :meth:`gspread.Worksheet.col_values`.

        Empty cells in this list will be rendered as :const:`None`.

        :param col: Column number.
        :type col: int
        :param `gspread.utils.ValueRenderOption` value_render_option:
            (optional) Determines how values should be
            rendered in the output. See
            `ValueRenderOption`_ in the Sheets API.
        :rtype: :py:class:`~typing.List`\\[:py:class:`~typing.Optional`\\[`str`\\]\\]
        """
        return await self.agcm._call(
            self.ws.col_values, col, value_render_option=value_render_option
        )

    @_nowait
    async def columns_auto_resize(self, start_column_index: int, end_column_index: int):
        """Updates the size of rows or columns in the worksheet.

        Index start from 0.

        :param int start_column_index: The index (inclusive) to begin resizing
        :param int end_column_index: The index (exclusive) to finish resizing
        :param bool nowait: (optional) If true, return a scheduled future instead
            of waiting for the API call to complete.

        .. versionadded:: 1.6
        """
        return await self.agcm._call(
            self.ws.columns_auto_resize, start_column_index, end_column_index
        )

    async def copy_to(self, spreadsheet_id: str) -> dict:
        """Copies this sheet to another spreadsheet.

        :param str spreadsheet_id: The ID of the spreadsheet to copy
            the sheet to.

        :returns: a dict with the response containing information about
            the newly created sheet.
        :rtype: dict

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ws.copy_to, spreadsheet_id)

    async def copy_range(
        self,
        source: str,
        dest: str,
        paste_type=gspread.utils.PasteType.normal,
        paste_orientation=gspread.utils.PasteOrientation.normal,
    ):
        """Copies a range of data from source to dest

            .. note::
                ``paste_type`` values are explained here: `Paste Types`_

        :param str source: The A1 notation of the source range to copy
        :param str dest: The A1 notation of the destination where to paste the data
            Can be the A1 notation of the top left corner where the range must be paste
            ex: G16, or a complete range notation ex: G16:I20.
            The dimensions of the destination range is not checked and has no effect,
            if the destination range does not match the source range dimension, the entire
            source range is copies anyway.
        :param paste_type: the paste type to apply. Many paste type are available from
            the Sheet API, see above note for detailed values for all values and their effects.
            Defaults to ``PasteType.normal``
        :type paste_type: `gspread.utils.PasteType`
        :param paste_orientation: The paste orient to apply.
            Possible values are: ``normal`` to keep the same orientation, ``transpose`` where all rows become columns and vice versa.
        :type paste_orientation: `gspread.utils.PasteOrientation`

        .. _Paste Types: https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets/request#pastetype

        .. versionadded:: 1.8
        """
        return await self.agcm._call(
            self.ws.copy_range,
            source,
            dest,
            paste_type=paste_type,
            paste_orientation=paste_orientation,
        )

    async def cut_range(
        self,
        source: str,
        dest: str,
        paste_type=gspread.utils.PasteType.normal,
    ):
        """Moves a range of data form source to dest

            .. note::
               ``paste_type`` values are explained here: `Paste Types`_

        :param str source: The A1 notation of the source range to move
        :param str dest: The A1 notation of the destination where to paste the data
            **it must be a single cell** in the A1 notation. ex: G16
        :param paste_type: the paste type to apply. Many paste type are available from
            the Sheet API, see above note for detailed values for all values and their effects.
            Defaults to ``PasteType.normal``
        :type paste_type: `gspread.utils.PasteType`

        .. _Paste Types: https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets/request#pastetype

        .. versionadded:: 1.8
        """

    async def define_named_range(self, name: str, range_name: str):
        """
        :param str name: A string with range value in A1 notation,
            e.g. 'A1:A5'.

        :param str range_name: The name to assign to the range of cells

        :returns: the response body from the request
        :rtype: dict

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ws.define_named_range, name, range_name)

    @_nowait
    async def delete_columns(self, start_index: int, end_index: int = None):
        """Deletes multiple columns from the worksheet at the specified index.

        :param int start_index: Index of a first column for deletion.
        :param int end_index: Index of a last column for deletion.
            When end_index is not specified this method only deletes a single
            column at ``start_index``.
        :param bool nowait: (optional) If true, return a scheduled future instead
            of waiting for the API call to complete.

        .. versionadded:: 1.6
        """
        return await self.agcm._call(
            self.ws.delete_columns, start_index, end_index=end_index
        )

    @_nowait
    async def delete_dimension(
        self, dimension: str, start_index: int, end_index: int = None
    ):
        """Deletes multi rows from the worksheet at the specified index.

        :param str dimension: A dimension to delete. ``Dimension.rows`` or ``Dimension.cols``.
        :param int start_index: Index of a first row for deletion.
        :param int end_index: Index of a last row for deletion. When
            ``end_index`` is not specified this method only deletes a single
            row at ``start_index``.
        :param bool nowait: (optional) If true, return a scheduled future instead
            of waiting for the API call to complete.

        .. versionadded:: 1.6
        """
        return await self.agcm._call(
            self.ws.delete_dimension, dimension, start_index, end_index=end_index
        )

    async def delete_named_range(self, named_range_id: str) -> dict:
        """
        :param str named_range_id: The ID of the named range to delete.
            Can be obtained with :meth:`AsyncioGspreadSpreadsheet.list_named_ranges`.
        :param bool nowait: (optional) If true, return a scheduled future instead
            of waiting for the API call to complete.

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ws.delete_named_range, named_range_id)

    async def delete_protected_range(self, id: str) -> dict:
        """Delete protected range identified by the ID ``id``.

        :param str id: The ID of the protected range to delete.
            Can be obtained with :meth:`AsyncioGspreadSpreadsheet.list_protected_ranges`.

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ws.delete_protected_range, id)

    @_nowait
    async def delete_rows(self, index: int, end_index: Optional[int] = None):
        """Deletes multiple rows from the worksheet starting at the specified
        index. Wraps :meth:`gspread.Worksheet.delete_rows`.

        :param int index: Index of a row for deletion.
        :param int end_index: Index of a last row for deletion.
            When end_index is not specified this method only deletes a single
            row at ``start_index``.
        :param bool nowait: (optional) If true, return a scheduled future instead of
            waiting for the API call to complete.

        .. versionadded:: 1.2
        """
        return await self.agcm._call(self.ws.delete_rows, index, end_index=end_index)

    async def find(
        self,
        query: "Union[str, re.Pattern]",
        in_row: Optional[int] = None,
        in_column: Optional[int] = None,
        case_sensitive: bool = True,
    ) -> "gspread.Cell":
        """Finds the first cell matching the query. Wraps
        :meth:`gspread.Worksheet.find`.

        :param query: A literal string to match or compiled regular expression.
        :type query: str, :py:class:`re.Pattern`
        :param int in_row: (optional) One-based row number to scope the search.
        :param int in_column: (optional) One-based column number to scope
            the search.
        :param bool case_sensitive: (optional) case sensitive string search.
            Default is True, does not apply to regular expressions.
        :rtype: :class:`gspread.Cell`
        """
        return await self.agcm._call(
            self.ws.find,
            query,
            in_row=in_row,
            in_column=in_column,
            case_sensitive=case_sensitive,
        )

    async def findall(
        self,
        query: "Union[str, re.Pattern]",
        in_row: Optional[int] = None,
        in_column: Optional[int] = None,
    ) -> List[gspread.Cell]:
        """Finds all cells matching the query. Wraps
        :meth:`gspread.Worksheet.find`.

        :param query: A literal string to match or compiled regular expression.
        :type query: str, :py:class:`re.Pattern`
        :param int in_row: (optional) One-based row number to scope the search.
        :param int in_column: (optional) One-based column number to scope
        :rtype: :py:class:`~typing.List`\\[:class:`gspread.Cell`\\]
        """
        return await self.agcm._call(
            self.ws.findall, query, in_row=in_row, in_column=in_column
        )

    async def format(self, ranges: Union[str, List[str]], format: dict) -> dict:
        """Format a list of ranges with the given format.

        :param str|list ranges: Target ranges in the A1 notation.
        :param dict format: Dictionary containing the fields to update.
            See `CellFormat`_ in the Sheets API for available fields.

        Examples::

            # Set 'A4' cell's text format to bold
            worksheet.format("A4", {"textFormat": {"bold": True}})

            # Set 'A1:D4' and 'A10:D10' cells's text format to bold
            worksheet.format(["A1:D4", "A10:D10"], {"textFormat": {"bold": True}})

            # Color the background of 'A2:B2' cell range in black,
            # change horizontal alignment, text color and font size
            worksheet.format("A2:B2", {
                "backgroundColor": {
                  "red": 0.0,
                  "green": 0.0,
                  "blue": 0.0
                },
                "horizontalAlignment": "CENTER",
                "textFormat": {
                  "foregroundColor": {
                    "red": 1.0,
                    "green": 1.0,
                    "blue": 1.0
                  },
                  "fontSize": 12,
                  "bold": True
                }
            })

        .. versionadded:: 1.6
        """
        return await self.agcm._call(
            self.ws.format,
            ranges,
            format,
        )

    async def freeze(self, rows: int = None, cols: int = None):
        """Freeze rows and/or columns on the worksheet.

        :param rows: Number of rows to freeze.
        :param cols: Number of columns to freeze.

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ws.format, rows=rows, cols=cols)

    @property
    def frozen_row_count(self) -> int:
        """Number of frozen rows.

        .. versionadded:: 1.6
        """
        return self.ws.frozen_row_count

    @property
    def frozen_col_count(self) -> int:
        """Number of frozen columns.

        .. versionadded:: 1.6
        """
        return self.ws.frozen_col_count

    async def get(
        self,
        range_name: str = None,
        major_dimension: str = None,
        value_render_option: gspread.utils.ValueRenderOption = None,
        date_time_render_option: gspread.utils.DateTimeOption = None,
        combine_merged_cells: bool = False,
    ):
        """Reads values of a single range or a cell of a sheet.

        :param str range_name: (optional) Cell range in the A1 notation or
            a named range.
        :param str major_dimension: (optional) The major dimension that results
            should use. Either ``ROWS`` or ``COLUMNS``.
        :param `gspread.utils.ValueRenderOption` value_render_option:
            (optional) How values should be
            represented in the output. The default render option is
            ``ValueRenderOption.formatted``.
        :param `gspread.utils.DateTimeOption` date_time_render_option: (optional) How dates, times, and
            durations should be represented in the output. This is ignored if
            ``value_render_option`` is ``ValueRenderOption.formatted``. The default
            ``date_time_render_option`` is ``SERIAL_NUMBER``.
        :param bool combine_merged_cells: (optional) If True, then all cells that
            are part of a merged cell will have the same value as the top-left
            cell of the merged cell. Defaults to False.

            .. warning::
                Setting this to True will cause an additional API request to be
                made to retrieve the values of all merged cells.

        :rtype: :class:`gspread.worksheet.ValueRange`

        Examples::

            # Return all values from the sheet
            worksheet.get()

            # Return value of 'A1' cell
            worksheet.get('A1')

            # Return values of 'A1:B2' range
            worksheet.get('A1:B2')

            # Return values of 'my_range' named range
            worksheet.get('my_range')

        .. versionadded:: 1.6
        """
        return await self.agcm._call(
            self.ws.get,
            range_name,
            major_dimension=major_dimension,
            value_render_option=value_render_option,
            date_time_render_option=date_time_render_option,
            combine_merged_cells=combine_merged_cells,
        )

    async def get_all_records(
        self,
        empty2zero: bool = False,
        head: int = 1,
        default_blank: str = "",
        allow_underscores_in_numeric_literals: bool = False,
        numericise_ignore: List[Union[int, str]] = [],
        value_render_option: gspread.utils.ValueRenderOption = None,
    ) -> List[dict]:
        """Returns a list of dictionaries, all of them having the contents
        of the spreadsheet with the head row as keys and each of these
        dictionaries holding the contents of subsequent rows of cells
        as values. Wraps :meth:`gspread.Worksheet.get_all_records`.

        Cell values are numericised (strings that can be read as ints
        or floats are converted).

        :param empty2zero: (optional) Determines whether empty cells are
                           converted to zeros.
        :type empty2zero: bool
        :param head: (optional) Determines which row to use as keys, starting
                     from 1 following the numeration of the spreadsheet.
        :type head: int
        :param default_blank: (optional) Determines whether empty cells are
                              converted to something else except empty string
                              or zero.
        :type default_blank: str
        :param bool allow_underscores_in_numeric_literals: (optional) Allow
             underscores in numeric literals, as introduced in PEP 515
        :param list numericise_ignore: (optional) List of ints of indices of
             the column (starting at 1) to ignore numericising, special use
             of ['all'] to ignore numericising on all columns.
        :param `gspread.utils.ValueRenderOption` value_render_option:
            (optional) Determines how values should be
            rendered in the output. See
            `ValueRenderOption`_ in the Sheets API.
        :rtype: :class:`~typing.List`\\[:class:`dict`\\]

        .. _ValueRenderOption: https://developers.google.com/sheets/api/reference/rest/v4/ValueRenderOption
        """
        return await self.agcm._call(
            self.ws.get_all_records,
            empty2zero=empty2zero,
            head=head,
            default_blank=default_blank,
            allow_underscores_in_numeric_literals=allow_underscores_in_numeric_literals,
            numericise_ignore=numericise_ignore,
            value_render_option=value_render_option,
        )

    async def get_all_values(self) -> List[List[str]]:
        """Returns a list of lists containing all cells' values as strings.
        Wraps :meth:`gspread.Worksheet.get_all_values`.

        :rtype: :class:`~typing.List`\\[:class:`~typing.List`\\[:class:`str`\\]\\]
        """
        return await self.agcm._call(self.ws.get_all_values)

    async def get_note(self, cell: str) -> str:
        """Get the content of the note located at cell, or the empty string
        if the cell does not have a note.

        :param str cell: A string with cell coordinates in A1 notation, e.g. ‘D7’.
        :rtype: :class:`str`

        .. versionadded:: 1.5
        """
        return await self.agcm._call(self.ws.get_note, cell)

    async def update_notes(self, notes: Dict):
        """Update multiple notes.

        :param dict notes: A dict of notes with their cell coordinates and respective content

            dict format is:

            * key: the cell coordinates as A1 range format
            * value: the string content of the cell

            Example::

                {
                    "D7": "Please read my notes",
                    "GH42": "this one is too far",
                }

        .. versionadded: 1.9
        """
        return await self.agcm._call(self.ws.update_notes, notes)

    async def get_values(
        self,
        range_name: str = None,
        major_dimension: str = None,
        value_render_option: gspread.utils.ValueRenderOption = None,
        date_time_render_option: gspread.utils.DateTimeOption = None,
        combine_merged_cells: bool = False,
    ) -> List[List]:
        """Returns a list of lists containing all values from specified range.
        By default values are returned as strings. See ``value_render_option``
        to change the default format.

        :param str range_name: (optional) Cell range in the A1 notation or
             a named range. If not specified the method returns values from all
             non empty cells.
        :param str major_dimension: (optional) The major dimension of the
             values. Either ``ROWS`` or ``COLUMNS``. Defaults to ``ROWS``
        :param `gspread.utils.ValueRenderOption` value_render_option:
             (optional) Determines how values should
             be rendered in the output. See `ValueRenderOption`_ in
             the Sheets API.
             Possible values are:

             ``FORMATTED_VALUE``
                  (default) Values will be calculated and formatted according
                  to the cell's formatting. Formatting is based on the
                  spreadsheet's locale, not the requesting user's locale.
             ``UNFORMATTED_VALUE``
                  Values will be calculated, but not formatted in the reply.
                  For example, if A1 is 1.23 and A2 is =A1 and formatted as
                  currency, then A2 would return the number 1.23.
             ``FORMULA``
                  Values will not be calculated. The reply will include
                  the formulas. For example, if A1 is 1.23 and A2 is =A1 and
                  formatted as currency, then A2 would return "=A1".

        :param `gspread.utils.ValueRenderOption` date_time_render_option: (optional) How dates, times, and
             durations should be represented in the output. This is ignored if
             ``value_render_option`` is ``FORMATTED_VALUE``. The default
             ``date_time_render_option`` is ``SERIAL_NUMBER``.

        .. note::
             Empty trailing rows and columns will not be included.

        :param bool combine_merged_cells: (optional) If True, then all cells that
            are part of a merged cell will have the same value as the top-left
            cell of the merged cell. Defaults to False.

            .. warning::

                Setting this to True will cause an additional API request to be
                made to retrieve the values of all merged cells.

        :rtype: :class:`~typing.List`\\[:class:`~typing.List`\\]

        .. versionadded:: 1.5

        .. _ValueRenderOption: https://developers.google.com/sheets/api/reference/rest/v4/ValueRenderOption
        """
        return await self.agcm._call(
            self.ws.get_values,
            range_name,
            major_dimension=major_dimension,
            value_render_option=value_render_option,
            date_time_render_option=date_time_render_option,
            combine_merged_cells=combine_merged_cells,
        )

    async def hide(self):
        """Hides the current worksheet from the UI.

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ws.hide)

    async def hide_columns(self, start: int, end: int):
        """
        Explicitly hide the given column index range.

        Index start from 0.

        :param int start: The (inclusive) starting column to hide
        :param int end: The (exclusive) end column to hide

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ws.hide_columns, start, end)

    async def hide_rows(self, start: int, end: int):
        """
        Explicitly hide the given row index range.

        Index start from 0.

        :param int start: The (inclusive) starting column to hide
        :param int end: The (exclusive) end column to hide

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ws.hide_rows, start, end)

    @property
    def id(self) -> int:
        """:returns: Worksheet ID.
        :rtype: int

        .. versionadded:: 1.6
        """
        return self.ws.id

    @property
    def index(self) -> int:
        """:returns: Worksheet index.
        :rtype: int

        .. versionadded:: 1.6
        """
        return self.ws.index

    @_nowait
    async def insert_cols(
        self,
        values: List[List],
        col: int = 1,
        value_input_option: gspread.utils.ValueInputOption = gspread.utils.ValueInputOption.raw,
        inherit_from_before: bool = False,
    ):
        """Adds multiple new cols to the worksheet at specified index and
        populates them with values. Wraps
        :meth:`gspread.Worksheet.insert_cols`.

        :param values: List of values for the new columns.
        :type values: :class:`~typing.List`\\[:class:`~typing.List`\\]
        :param int col: (optional) Offset for the newly inserted columns.
        :param value_input_option: (optional) Determines how values should be
            rendered in the output. Possible values are ``RAW`` or
            ``USER_ENTERED``. See `ValueInputOption`_ in the Sheets API.
        :type value_input_option: `gspread.utils.ValueInputOption`
        :param bool inherit_from_before: (optional) If True, new columns will
            inherit their properties from the previous column. Defaults to
            False, meaning that new columns acquire the properties of the
            column immediately after them.

            .. warning::

               `inherit_from_before` must be False if adding at the left edge
               of a spreadsheet (`col=1`), and must be True if adding at the
               right edge of the spreadsheet.

        :param bool nowait: (optional) If true, return a scheduled future instead of waiting for the API call to complete.

        .. _ValueInputOption: https://developers.google.com/sheets/api/reference/rest/v4/ValueInputOption
        .. versionadded:: 1.4
        """
        return await self.agcm._call(
            self.ws.insert_cols,
            values,
            col=col,
            value_input_option=value_input_option,
            inherit_from_before=inherit_from_before,
        )

    @_nowait
    async def insert_note(self, cell: str, content: str) -> None:
        """Insert a note. The note is attached to a certain cell.

        :param str cell: A string with a cell coordinates in A1 notation,
            e.g. 'D7'.

        :param str content: The text note to insert.
        :param bool nowait: (optional) If true, return a scheduled future instead of waiting for the API call to complete.

        .. versionadded:: 1.4
        """
        return await self.agcm._call(self.ws.insert_note, cell, content)

    @_nowait
    async def insert_notes(self, notes: Dict):
        """Insert multiple notes.

        :param dict notes: A dict of notes with their cells coordinates and respective content

            dict format is:

            * key: the cell coordinates as A1 range format
            * value: the string content of the cell

            Example::

                {
                    "D7": "Please read my notes",
                    "GH42": "this one is too far",
                }

        .. versionadded:: 1.9
        """
        return await self.agcm._call(self.ws.insert_notes, notes)

    @_nowait
    async def clear_notes(self, ranges: List[str]):
        """Clear all notes located at the cells in `ranges`.

        :param ranges: List of A1 coordinates to clear notes
        :type ranges: :class:`~typing.List`\\[:class:`str`\\]

        .. versionadded:: 1.9
        """
        return await self.agcm._call(self.ws.clear_notes, ranges)

    @_nowait
    async def insert_row(
        self,
        values: List,
        index: int = 1,
        value_input_option: gspread.utils.ValueInputOption = gspread.utils.ValueInputOption.raw,
        inherit_from_before: bool = False,
    ):
        """Adds a row to the worksheet at the specified index
        and populates it with values. Wraps
        :meth:`gspread.Worksheet.insert_row`.

        Widens the worksheet if there are more values than columns.

        :param values: List of values for the new row.
        :type values: :class:`~typing.List`
        :param index: (optional) Offset for the newly inserted row.
        :type index: int
        :param `gspread.utils.ValueInputOption` value_input_option:
            (optional) Determines how values should be
            rendered in the output. See
            `ValueInputOption`_ in the Sheets API.
        :param bool inherit_from_before: (optional) If True, the new row will
            inherit its properties from the previous row. Defaults to False,
            meaning that the new row acquires the properties of the row
            immediately after it.

            .. warning::

               `inherit_from_before` must be False when adding a row to the top
               of a spreadsheet (`index=1`), and must be True when adding to
               the bottom of the spreadsheet.

        :param bool nowait: (optional) If true, return a scheduled future instead of waiting for the API call to complete.

        .. _ValueInputOption: https://developers.google.com/sheets/api/reference/rest/v4/ValueInputOption
        """
        return await self.agcm._call(
            self.ws.insert_row,
            values,
            index=index,
            value_input_option=value_input_option,
            inherit_from_before=inherit_from_before,
        )

    @_nowait
    async def insert_rows(
        self,
        values: List[List],
        row: int = 1,
        value_input_option: gspread.utils.ValueInputOption = gspread.utils.ValueInputOption.raw,
        inherit_from_before: bool = False,
    ):
        """Adds multiple rows to the worksheet at the specified index and
        populates them with values.

        :param list values: List of row lists. a list of lists, with the lists
            each containing one row's values. Widens the worksheet if there are
            more values than columns.
        :type values: :class:`~typing.List`\\[:class:`~typing.List`\\]
        :param int row: Start row to update (one-based). Defaults to 1 (one).
        :param `gspread.utils.ValueInputOption` value_input_option:
            (optional) Determines how input data
            should be interpreted. Possible values are ``RAW`` or
            ``USER_ENTERED``. See `ValueInputOption`_ in the Sheets API.
        :param bool inherit_from_before: (optional) If True, the new row will
            inherit its properties from the previous row. Defaults to False,
            meaning that the new row acquires the properties of the row
            immediately after it.

            .. warning::

               `inherit_from_before` must be False when adding a row to the top
               of a spreadsheet (`index=1`), and must be True when adding to
               the bottom of the spreadsheet.

        :param bool nowait: (optional) If true, return a scheduled future instead of waiting for the API call to complete.

        .. versionadded:: 1.1
        """
        return await self.agcm._call(
            self.ws.insert_rows,
            values,
            row=row,
            value_input_option=value_input_option,
            api_call_count=2,
            inherit_from_before=inherit_from_before,
        )

    async def list_dimension_group_columns(self) -> List[dict]:
        """
        List all the grouped columns in this worksheet

        :returns: list of the grouped columns
        :rtype: list

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ws.list_dimension_group_columns)

    async def list_dimension_group_rows(self) -> List[dict]:
        """
        List all the grouped rows in this worksheet

        :returns: list of the grouped rows
        :rtype: list

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ws.list_dimension_group_rows)

    async def merge_cells(self, name: str, merge_type: str = "MERGE_ALL"):
        """Merge cells. There are 3 merge types: ``MERGE_ALL``, ``MERGE_COLUMNS``,
        and ``MERGE_ROWS``.

        :param str name: Range name in A1 notation, e.g. 'A1:A5'.
        :param str merge_type: (optional) one of ``MERGE_ALL``,
            ``MERGE_COLUMNS``, or ``MERGE_ROWS``. Defaults to ``MERGE_ROWS``.
            See `MergeType`_ in the Sheets API reference.

        :returns: the response body from the request
        :rtype: dict

        .. _MergeType: https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets/request#MergeType

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ws.merge_cells, name, merge_type=merge_type)

    async def range(self, *args, **kwargs) -> List[gspread.Cell]:
        """Returns a list of :class:`~gspread.Cell` objects from a specified
        range. Wraps :meth:`gspread.Worksheet.range`.

        :param str name: A string with range value in A1 notation, e.g. 'A1:A5' or the
             named range to fetch.

        Alternatively, you may specify numeric boundaries. All values
        index from 1 (one):

        :param int first_row: Row number
        :param int first_col: Row number
        :param int last_row: Row number
        :param int last_col: Row number

        :rtype: :class:`~typing.List`\\[:class:`gspread.Cell`\\]
        """
        return await self.agcm._call(self.ws.range, *args, **kwargs)

    @_nowait
    async def resize(self, rows: Optional[int] = None, cols: Optional[int] = None):
        """Resizes the worksheet. Specify one of ``rows`` or ``cols``.
        Wraps :meth:`gspread.Worksheet.resize`.

        :param int rows: (optional) New number of rows.
        :param int cols: (optional) New number columns.
        :param bool nowait: (optional) If true, return a scheduled future instead of
            waiting for the API call to complete.
        """
        return await self.agcm._call(self.ws.resize, rows=rows, cols=cols)

    @property
    def row_count(self) -> int:
        """:returns: Number of rows in the worksheet.
        :rtype: int
        """
        return self.ws.row_count

    async def row_values(
        self,
        row: int,
        major_dimension=None,
        value_render_option: gspread.utils.ValueRenderOption = None,
        date_time_render_option: gspread.utils.DateTimeOption = None,
    ) -> list:
        """Returns a list of all values in a `row`. Wraps
        :meth:`gspread.Worksheet.row_values`.

        Empty cells in this list will be rendered as :const:`None`.

        :param int row: Row number.
        :param `gspread.utils.ValueRenderOption` value_render_option:
            (optional) Determines how values should be
            rendered in the output. See
            `ValueRenderOption`_ in the Sheets API.
        :param `gspread.utils.DateTimeOption` date_time_render_option: (optional) How dates, times, and
            durations should be represented in the output.


        .. _ValueRenderOption: https://developers.google.com/sheets/api/reference/rest/v4/ValueRenderOption
        """
        return await self.agcm._call(
            self.ws.row_values,
            row,
            major_dimension=major_dimension,
            value_render_option=value_render_option,
            date_time_render_option=date_time_render_option,
        )

    async def rows_auto_resize(self, start_row_index: int, end_row_index: int):
        """Updates the size of rows or columns in the  worksheet.

        Index start from 0

        :param start_row_index: The index (inclusive) to begin resizing
        :param end_row_index: The index (exclusive) to finish resizing

        .. versionadded:: 1.6
        """
        return await self.agcm._call(
            self.ws.rows_auto_resize, start_row_index, end_row_index
        )

    async def set_basic_filter(self, name: str):
        """Add a basic filter to the worksheet. If a range or boundaries
        are passed, the filter will be limited to the given range.

        :param str name: A string with range value in A1 notation,
            e.g. ``A1:A5``.

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ws.set_basic_filter, name=name)

    async def show(self):
        """Show the current worksheet in the UI.

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ws.show)

    async def hide_gridlines(self):
        """Hide gridlines on the current worksheet

        .. versionadded:: 1.9
        """
        return await self.agcm._call(self.ws.hide_gridlines)

    async def show_gridlines(self):
        """Show gridlines on the current worksheet

        .. versionadded:: 1.9
        """
        return await self.agcm._call(self.ws.show_gridlines)

    async def sort(self, specs: List[Tuple[int, str]], range: str = None):
        """Sorts worksheet using given sort orders.

        :param list specs: The sort order per column. Each sort order
            represented by a tuple where the first element is a column index
            and the second element is the order itself: 'asc' or 'des'.
        :param str range: The range to sort in A1 notation. By default sorts
            the whole sheet excluding frozen rows.

        Example::

            # Sort sheet A -> Z by column 'B'
            wks.sort((2, 'asc'))

            # Sort range A2:G8 basing on column 'G' A -> Z
            # and column 'B' Z -> A
            wks.sort((7, 'asc'), (2, 'des'), range='A2:G8')

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ws.sort, *specs, range=range)

    @property
    def title(self) -> str:
        """:returns: Human-readable worksheet title.
        :rtype: str
        """
        return self.ws.title

    async def unhide_columns(self, start: int, end: int):
        """
        Explicitly unhide the given column index range.

        Index start from 0.

        :param int start: The (inclusive) starting column to hide
        :param int end: The (exclusive) end column to hide

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ws.unhide_columns, start, end)

    async def unhide_rows(self, start: int, end: int):
        """
        Explicitly unhide the given row index range.

        Index start from 0.

        :param int start: The (inclusive) starting row to hide
        :param int end: The (exclusive) end row to hide

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ws.unhide_rows, start, end)

    async def unmerge_cells(self, name: str):
        """Unmerge cells.

        Unmerge previously merged cells.

        :param str name: Range name in A1 notation, e.g. 'A1:A5'.

        :returns: the response body from the request
        :rtype: dict

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ws.unmerge_cells, name)

    @_nowait
    async def update(
        self,
        values: List[List],
        range_name: Optional[str] = None,
        raw=True,
        major_dimension: str = None,
        value_input_option: gspread.utils.ValueInputOption = None,
        include_values_in_response=None,
        response_value_render_option: gspread.utils.ValueRenderOption = None,
        response_date_time_render_option: gspread.utils.DateTimeOption = None,
    ):
        """Sets values in a cell range of the sheet. Wraps
        :meth:`gspread.Worksheet.update`.

        :param list values: The data to be written.
        :param str range_name: The A1 notation of the values
             to update.
        :param bool raw: The values will not be parsed by Sheets API and will
             be stored as-is. For example, formulas will be rendered as plain
             strings. Defaults to ``True``. This is a shortcut for
             the ``value_input_option`` parameter.
        :param str major_dimension: (optional) The major dimension of the
             values. Either ``ROWS`` or ``COLUMNS``.
        :param `gspread.utils.ValueInputOption` value_input_option:
             (optional) How the input data should be
             interpreted. Possible values are:

             ``RAW``
                  The values the user has entered will not be parsed and will be
                  stored as-is.
             ``USER_ENTERED``
                  The values will be parsed as if the user typed them into the
                  UI. Numbers will stay as numbers, but strings may be converted
                  to numbers, dates, etc. following the same rules that are
                  applied when entering text into a cell via
                  the Google Sheets UI.
        :param bool nowait: (optional) If true, return a scheduled future instead of waiting for the API call to complete.

        .. versionadded:: 1.5
        """
        return await self.agcm._call(
            self.ws.update,
            values,
            range_name=range_name,
            raw=raw,
            major_dimension=major_dimension,
            value_input_option=value_input_option,
            include_values_in_response=include_values_in_response,
            response_value_render_option=response_value_render_option,
            response_date_time_render_option=response_date_time_render_option,
        )

    @_nowait
    async def update_acell(self, label: str, value):
        """Updates the value of a cell. Wraps
        :meth:`gspread.Worksheet.row_values`.

        :param str label: Cell label in A1 notation.
            Letter case is ignored.
        :param value: New value.
        :param bool nowait: (optional) If true, return a scheduled future instead of
            waiting for the API call to complete.
        """
        row, col = a1_to_rowcol(label)
        return await self.update_cell(row, col, value)

    @_nowait
    async def update_cell(self, row: int, col: int, value):
        """Updates the value of a cell.

        :param int row: Row number.
        :param int col: Column number.
        :param value: New value.
        :param bool nowait: (optional) If true, return a scheduled future
            instead of waiting for the API call to complete.
        """
        return await self.agcm._call(self.ws.update_cell, row, col, value)

    @_nowait
    async def update_cells(
        self,
        cell_list: List[gspread.Cell],
        value_input_option: gspread.utils.ValueInputOption = gspread.utils.ValueInputOption.raw,
    ):
        """Updates many cells at once. Wraps
        :meth:`gspread.Worksheet.update_cells`.

        :param cell_list: List of :class:`~gspread.Cell` objects to update.
        :param `gspread.utils.ValueInputOption` value_input_option:
            (optional) Determines how values should be
            rendered in the output. See
            `ValueRenderOption`_ in the Sheets API.
        :param bool nowait: (optional) If true, return a scheduled future instead of waiting for the API call to complete.

        .. _ValueInputOption: https://developers.google.com/sheets/api/reference/rest/v4/ValueInputOption
        """
        return await self.agcm._call(
            self.ws.update_cells, cell_list, value_input_option=value_input_option
        )

    async def update_index(self, index: int):
        """Updates the ``index`` property for the worksheet.

        See the `Sheets API documentation
        <https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets#sheetproperties>`_
        for information on how updating the index property affects the order of worksheets
        in a spreadsheet.

        To reorder all worksheets in a spreadsheet, see `AsyncioGspreadSpreadsheet.reorder_worksheets`.

        .. versionadded:: 1.6
        """
        return await self.agcm._call(self.ws.update_index, index)

    @_nowait
    async def update_note(self, cell: str, content: str) -> None:
        """Update the content of the note located at `cell`.

        :param str cell: A string with cell coordinates in A1 notation, e.g. 'D7'.
        :param str content: The text note to insert.
        :param bool nowait: (optional) If true, return a scheduled future instead of waiting for the API call to complete.

        .. versionadded:: 1.4
        """
        return await self.agcm._call(self.ws.update_note, cell, content)

    async def update_title(self, title):
        raise NotImplemented("This breaks ws caching, could be implemented later")

    @_nowait
    async def update_tab_color(self, color: dict):
        """Changes the worksheet's tab color.

        :param dict color: The red, green and blue values of the color, between 0 and 1.
        :param bool nowait: (optional) If true, return a scheduled future instead of waiting for the API call to complete.

        .. versionadded:: 1.7.0
        """
        return await self.agcm._call(self.ws.update_tab_color, color)

    @property
    def url(self) -> str:
        """:returns: Worksheet URL.
        :rtype: str

        .. versionadded:: 1.6
        """
        return self.ws.url


__all__ = [
    "AsyncioGspreadClientManager",
    "AsyncioGspreadClient",
    "AsyncioGspreadSpreadsheet",
    "AsyncioGspreadWorksheet",
]
