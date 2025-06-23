from note import sum_list_elements
import unittest

class TestSumListElements(unittest.TestCase):
    
    def test_positive_numbers(self):
        self.assertEqual(sum_list_elements([1, 2, 3, 4, 5]), 15)

    def test_negative_numbers(self):
        self.assertEqual(sum_list_elements([-1, -2, -3]), -6)

    def test_mixed_numbers(self):
        self.assertEqual(sum_list_elements([10, -5, 3, -8]), 0)

    def test_empty_list(self):
        self.assertEqual(sum_list_elements([]), 0)

    def test_single_element(self):
        self.assertEqual(sum_list_elements([42]), 42)

if __name__ == '__main__':
    unittest.main()
