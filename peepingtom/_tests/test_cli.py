from click.testing import CliRunner

from peepingtom.__main__ import cli

runner = CliRunner()


def test_cli():
    runner.invoke(cli, 'test.mrc test.star -m bunch -n reg --no-show'.split())
