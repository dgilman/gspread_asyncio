# Configuration file for the Sphinx documentation builder.
#
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Project information -----------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information

import datetime
import os
import sys

sys.path.insert(0, os.path.abspath(".."))

TODAY = datetime.date.today()

project = "gspread_asyncio"
copyright = f"{TODAY.year}, David Gilman"
author = "David Gilman"

# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "sphinxcontrib_trio",
    "m2r2",
]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# -- Options for HTML output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

html_theme = "alabaster"
html_static_path = ["_static"]

html_theme_options = {
    "description": "An asyncio API for Google Spreadsheets",
    "github_button": True,
    "github_repo": "gspread_asyncio",
    "github_user": "dgilman",
    "fixed_sidebar": True,
}

# -- Options for intersphinx extension ---------------------------------------
# https://www.sphinx-doc.org/en/master/usage/extensions/intersphinx.html#configuration

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "oauth2client": ("https://oauth2client.readthedocs.io/en/latest/", None),
    "gspread": ("https://docs.gspread.org/en/latest/", None),
    "requests": ("https://requests.readthedocs.io/en/latest/", None),
    "google-auth": ("https://google-auth.readthedocs.io/en/latest/", None),
}

autodoc_typehints = "none"
