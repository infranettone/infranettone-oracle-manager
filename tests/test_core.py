"""El núcleo Python debe dar lo mismo que el de Windows con el fixture compartido (windows/Tests.cs)."""

import unittest
from pathlib import Path

from oracle_manager.discover import REMOTE_SCRIPT, parse
from oracle_manager.dump import DUMP_SCRIPT
from oracle_manager.usage import idle_sort, idle_text

FIXTURES = Path(__file__).parent / "fixtures"


class Core(unittest.TestCase):
    def test_fixture(self):
        i = parse((FIXTURES / "sample.txt").read_text(encoding="utf-8"))[0]
        self.assertEqual(i.errors, ["ORA-00942: table or view does not exist"])
        rows = ["\t".join((str(c.con_id), u.name, "Y" if u.oracle_maintained else "N", u.level, idle_text(u),
                           str(idle_sort(u))))
                for c in i.containers for u in c.users]
        self.assertEqual(rows, (FIXTURES / "sample.expected.tsv").read_text(encoding="utf-8").splitlines())

    def test_scripts(self):
        self.assertIn("_pmon_", REMOTE_SCRIPT)
        self.assertIn("expdp", DUMP_SCRIPT)
        self.assertNotIn("\r", REMOTE_SCRIPT + DUMP_SCRIPT)


if __name__ == "__main__":
    unittest.main()
