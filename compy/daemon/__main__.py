"""Module entrypoint: `python -m compy.daemon [input.json]`."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
