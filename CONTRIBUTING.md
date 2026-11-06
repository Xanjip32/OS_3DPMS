# Contributing

Thanks for helping. Bug reports, printer compatibility notes and pull requests are all welcome.

## Ground rules

- **Standard library only.** No third-party Python packages, so installation stays one click
  and the attack surface stays small.
- **Security first.** Do not add listeners on anything other than `127.0.0.1`, inline scripts,
  or endpoints that skip the identity and same-origin checks. Read [SECURITY.md](SECURITY.md).
- Keep Python 3.8 compatibility.

## Development

```bash
python -m unittest discover -s tests -t . -v
python tests/mock_printer.py 9001          # fake Moonraker printer on port 9001
python run.py --config my-config.json --data-dir ./dev-data --open-setup
```

Add a test for every bug fix and every new endpoint. CI runs the tests on Windows and Linux.

## Pull requests

1. Fork and create a branch.
2. Keep the change focused and describe how you tested it (which printer and firmware).
3. Update `CHANGELOG.md` under "Unreleased".

## Adding a printer type

Add a module next to `dashboard/klipper.py` and `dashboard/bambu.py` with `status`, `upload`
and a camera source, register the type in `config.py`, and add tests with a fake printer.
