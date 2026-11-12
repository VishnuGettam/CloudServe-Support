"""CloudServe support triage.

Loads settings from a .env file in the folder the command is run from (the
project root), if one exists. Variables already set in the environment win.
"""

try:
    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv(usecwd=True))
except ImportError:  # python-dotenv is optional; plain environment variables still work
    pass


def main() -> int:
    """Entry point for the `cloudserve-support` command: runs the batch harness."""
    from cloudserve_support.harness.__main__ import main as harness_main  # imported here to avoid a circular import

    return harness_main()