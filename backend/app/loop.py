"""Psycopg async requires a selector loop on Windows as well as Unix."""

import asyncio


def create_loop():
    return asyncio.SelectorEventLoop()
