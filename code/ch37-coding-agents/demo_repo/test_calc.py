import unittest

from calc import add, mul


class TestCalc(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(1, 2), 3)

    def test_add_zero(self):
        self.assertEqual(add(0, 0), 0)

    def test_mul(self):
        self.assertEqual(mul(3, 4), 12)


if __name__ == "__main__":
    unittest.main()
