import asyncio
import unittest
from unittest import mock

import google.auth.exceptions
import gspread
import requests

import gspread_asyncio


def async_test(f):
    def wrapper(*args, **kwargs):
        asyncio.run(f(*args, **kwargs), debug=True)

    return wrapper


def api_error(status_code):
    response = mock.Mock(spec=requests.Response)
    response.status_code = status_code
    response.json.return_value = {"error": {"code": status_code, "message": "nope"}}
    return gspread.exceptions.APIError(response)


def drive_api_error(status_code, reason):
    """The Drive API answers with the legacy Google JSON error shape, which
    carries a machine readable reason that the Sheets API does not send."""
    response = mock.Mock(spec=requests.Response)
    response.status_code = status_code
    response.json.return_value = {
        "error": {
            "code": status_code,
            "message": "nope",
            "errors": [
                {"domain": "usageLimits", "reason": reason, "message": "nope"},
            ],
        }
    }
    return gspread.exceptions.APIError(response)


class CountingClientManager(gspread_asyncio.ClientManager):
    """Records handler calls and does not sleep between retries."""

    def __init__(self):
        super().__init__(lambda: None, gspread_delay=0)
        self.gspread_errors = []
        self.requests_errors = []

    async def handle_gspread_error(self, e, method, args, kwargs):
        self.gspread_errors.append(e)

    async def handle_requests_error(self, e, method, args, kwargs):
        self.requests_errors.append(e)


class Flaky:
    """A stand-in for a gspread method. Raises every exception in ``errors``,
    one per call, then returns ``sentinel``."""

    __name__ = "flaky"

    def __init__(self, errors, sentinel="ok"):
        self.errors = list(errors)
        self.sentinel = sentinel
        self.call_count = 0

    def __call__(self, *args, **kwargs):
        self.call_count += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.sentinel


class RetryTest(unittest.TestCase):
    @async_test
    async def test_transport_error_is_retried(self):
        """google-auth re-raises a socket layer error from a lazy token
        refresh as a TransportError. It must be retried, not thrown at the
        caller."""
        cm = CountingClientManager()
        errors = [
            google.auth.exceptions.TransportError(requests.ReadTimeout("timed out"))
            for _ in range(2)
        ]
        method = Flaky(errors)

        rval = await cm._call(method)

        self.assertEqual(rval, "ok")
        self.assertEqual(method.call_count, 3)
        self.assertEqual(len(cm.requests_errors), 2)
        for e in cm.requests_errors:
            self.assertIsInstance(e, google.auth.exceptions.TransportError)

    @async_test
    async def test_requests_error_is_retried(self):
        cm = CountingClientManager()
        method = Flaky([requests.ConnectionError("no route to host")])

        rval = await cm._call(method)

        self.assertEqual(rval, "ok")
        self.assertEqual(method.call_count, 2)
        self.assertEqual(len(cm.requests_errors), 1)

    @async_test
    async def test_server_error_is_retried(self):
        cm = CountingClientManager()
        method = Flaky([api_error(500)])

        rval = await cm._call(method)

        self.assertEqual(rval, "ok")
        self.assertEqual(method.call_count, 2)
        self.assertEqual(len(cm.gspread_errors), 1)

    @async_test
    async def test_rate_limit_is_retried(self):
        cm = CountingClientManager()
        method = Flaky([api_error(429)])

        rval = await cm._call(method)

        self.assertEqual(rval, "ok")
        self.assertEqual(method.call_count, 2)
        self.assertEqual(len(cm.gspread_errors), 1)

    @async_test
    async def test_client_error_is_fatal(self):
        """HTTP 400 range codes other than 429 come from bad usage of the API.
        Retrying them hides the mistake."""
        cm = CountingClientManager()
        method = Flaky([api_error(403)])

        with self.assertRaises(gspread.exceptions.APIError):
            await cm._call(method)

        self.assertEqual(method.call_count, 1)
        self.assertEqual(cm.gspread_errors, [])

    @async_test
    async def test_drive_rate_limit_is_retried(self):
        """The Drive API signals rate limiting with a 403, not a 429."""
        for reason in ("rateLimitExceeded", "userRateLimitExceeded"):
            with self.subTest(reason=reason):
                cm = CountingClientManager()
                method = Flaky([drive_api_error(403, reason)])

                rval = await cm._call(method)

                self.assertEqual(rval, "ok")
                self.assertEqual(method.call_count, 2)
                self.assertEqual(len(cm.gspread_errors), 1)

    @async_test
    async def test_other_drive_403_is_fatal(self):
        """Every other Drive 403 is a quota, policy or permission error that no
        amount of retrying fixes."""
        for reason in (
            "sharingRateLimitExceeded",
            "dailyLimitExceeded",
            "storageQuotaExceeded",
            "insufficientFilePermissions",
        ):
            with self.subTest(reason=reason):
                cm = CountingClientManager()
                method = Flaky([drive_api_error(403, reason)])

                with self.assertRaises(gspread.exceptions.APIError):
                    await cm._call(method)

                self.assertEqual(method.call_count, 1)
                self.assertEqual(cm.gspread_errors, [])

    @async_test
    async def test_unparseable_error_body_is_fatal(self):
        """gspread synthesizes an error dict with no errors key when the
        response body is not JSON. The reason check must not throw over it."""
        response = mock.Mock(spec=requests.Response)
        response.status_code = 403
        response.json.side_effect = ValueError("not json")
        response.text = "<html>go away</html>"
        cm = CountingClientManager()
        method = Flaky([gspread.exceptions.APIError(response)])

        with self.assertRaises(gspread.exceptions.APIError):
            await cm._call(method)

        self.assertEqual(method.call_count, 1)

    @async_test
    async def test_refresh_error_is_fatal(self):
        """A RefreshError means the credentials are bad. Retrying it forever
        hides a fatal misconfiguration."""
        cm = CountingClientManager()
        method = Flaky([google.auth.exceptions.RefreshError("invalid_grant")])

        with self.assertRaises(google.auth.exceptions.RefreshError):
            await cm._call(method)

        self.assertEqual(method.call_count, 1)
        self.assertEqual(cm.requests_errors, [])

    @async_test
    async def test_retryable_refresh_error_is_retried(self):
        """google-auth sets retryable when the token endpoint answered with a
        5xx or a temporarily_unavailable style OAuth error. That is a transient
        fault on Google's side, not a bad credential."""
        cm = CountingClientManager()
        errors = [
            google.auth.exceptions.RefreshError(
                "temporarily_unavailable", retryable=True
            )
            for _ in range(2)
        ]
        method = Flaky(errors)

        rval = await cm._call(method)

        self.assertEqual(rval, "ok")
        self.assertEqual(method.call_count, 3)
        self.assertEqual(len(cm.requests_errors), 2)
        for e in cm.requests_errors:
            self.assertIsInstance(e, google.auth.exceptions.RefreshError)
            self.assertTrue(e.retryable)

    @async_test
    async def test_retryable_refresh_error_releases_the_call_lock(self):
        cm = CountingClientManager()
        method = Flaky(
            [google.auth.exceptions.RefreshError("server_error", retryable=True)]
        )

        await cm._call(method)

        self.assertFalse(cm.call_lock.locked())

    @async_test
    async def test_call_lock_is_released_after_a_fatal_error(self):
        cm = CountingClientManager()
        method = Flaky([api_error(403)])

        with self.assertRaises(gspread.exceptions.APIError):
            await cm._call(method)

        self.assertFalse(cm.call_lock.locked())


if __name__ == "__main__":
    unittest.main()
