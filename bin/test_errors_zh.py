import unittest
import errors_zh


class ChineseErrorTests(unittest.TestCase):
    def test_known_english_error_gets_reason_and_next_step(self):
        text = 'Traceback (most recent call last):\n  File "x.py"\nValueError: Resolve is rendering; retry after it finishes\n'
        reason, step, raw = errors_zh.explain(text)
        self.assertEqual((reason, step), ('达芬奇正在渲染', '等渲染完成后再保存'))
        self.assertEqual(raw, 'ValueError: Resolve is rendering; retry after it finishes')

    def test_chinese_message_is_kept_as_is(self):
        reason, _, _ = errors_zh.explain('ValueError: 达芬奇里的主线「X」有手工修改。')
        self.assertEqual(reason, '达芬奇里的主线「X」有手工修改。')

    def test_unknown_error_asks_to_send_details(self):
        reason, step, raw = errors_zh.explain('KeyError: frames')
        self.assertIn('没有预料', reason)
        self.assertEqual(raw, 'KeyError: frames')

    def test_report_mentions_log(self):
        self.assertTrue(errors_zh.report('OSError: [Errno 28] No space left on device', '/x/错误日志.txt').startswith('出错了：磁盘空间不足'))


class NestedErrorTests(unittest.TestCase):
    def test_reason_inside_an_exception_group_beats_the_wrapper(self):
        text = ('Traceback\n  | ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)\n'
                '    | ValueError: 达芬奇里的主线「A」已有手工修改，不需要再保存\n'
                "subprocess.CalledProcessError: Command '['python', 'import_mvp_delivery.py']' returned non-zero exit status 1.\n")
        self.assertEqual(errors_zh.explain(text)[0], '达芬奇里的主线「A」已有手工修改，不需要再保存')


if __name__ == '__main__':
    unittest.main()
