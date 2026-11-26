import unittest

from fourmilab import mcp


class WorkerBoundary(unittest.TestCase):
    def call(self, method, params):
        return mcp.answer({'jsonrpc': '2.0', 'id': 3, 'method': method, 'params': params},
                          lambda: {'run': {'id': 'only-this-run'}, 'catalog': []})

    def test_read_only_view_and_no_cross_run_selector(self):
        self.assertIn('only-this-run', self.call('tools/call', {'name': 'run_status'})['result']['content'][0]['text'])
        for method, args in [('stage', {}), ('cancel', {}), ('tools/call', {'name': 'shell'}),
                             ('tools/call', {'name': 'run_status', 'arguments': {'run': 'different'}}),
                             ('tools/call', {'name': 'run_status', 'arguments': {'path': '/operator'}})]:
            with self.subTest(method=method, args=args):
                self.assertIn('error', self.call(method, args))
