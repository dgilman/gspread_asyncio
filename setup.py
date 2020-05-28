import os.path

import setuptools


def read(filename):
    return open(os.path.join(os.path.dirname(__file__), filename)).read()


try:
    version = read("version_tag").strip()
except OSError:
    version = "1.99dev"

setuptools.setup(
    name="gspread_asyncio",
    version=version,
    description="asyncio wrapper for burnash's Google Spreadsheet API library, gspread",
    long_description=read("README.md"),
    long_description_content_type="text/markdown",
    url="https://github.com/dgilman/gspread_asyncio",
    author="David Gilman",
    author_email="dgilman@gilslotd.com",
    license="MIT",
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Framework :: AsyncIO",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3 :: Only",
    ],
    keywords=["spreadsheets", "google-spreadsheets", "asyncio"],
    project_urls={
        "Documentation": "https://gspread-asyncio.readthedocs.io/en/latest/",
        "Source": "https://github.com/dgilman/gspread_asyncio",
        "Tracker": "https://github.com/dgilman/gspread_asyncio/issues",
    },
    python_requires=">=3.5",
    packages=setuptools.find_packages(),
    install_requires=["requests==2.*", "gspread==3.6.*"],
)
