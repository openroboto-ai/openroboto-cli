"""One module per subcommand; the module name is the command name.

Each module exposes exactly two things:
- `add_parser(subparsers)` -- registers the arguments and calls
  `set_defaults(handler=run)`;
- `run(args) -> int` -- does the work and returns the exit code.
"""
