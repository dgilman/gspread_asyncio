.. mdinclude:: ../README.md

.. module:: gspread_asyncio

API reference
=============

Exceptions
----------
The :mod:`gspread_asyncio` module does not have any exceptions of its own, it instead propagates exceptions thrown from within :mod:`gspread` calls. These are all derived from a base class :exc:`gspread.exceptions.GSpreadException` and it is recommended that you catch and handle these errors.

Socket, network and rate limiting exceptions are handled by :mod:`gspread_asyncio` internally. The defaults are sensible but you can subclass several methods of :mod:`~gspread_asyncio.ClientManager` if you need to customize that behavior.


ClientManager
-------------
.. autoclass:: ClientManager
   :members:

Client
------
.. autoclass:: Client
   :members:

Spreadsheet
-----------
.. autoclass:: Spreadsheet
   :members:

Worksheet
---------
.. autoclass:: Worksheet
   :members:


Cell
----
See :class:`gspread.cell.Cell`.
