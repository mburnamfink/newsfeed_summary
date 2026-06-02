"""Backwards-compatible entry point. Prefer the `newsfeed` console script."""
from newsfeed.cli import main

if __name__ == "__main__":
    main()
