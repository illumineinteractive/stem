#!/usr/bin/env python
# Copyright 2011-2018, Damian Johnson and The Tor Project
# See LICENSE for licensing information

"""
Runs unit and integration tests. For usage information run this with '--help'.
"""

import os
import sys
import threading
import time
import traceback
import unittest

try:
  # TODO: added in python 2.7, drop check when removing 2.6 support

  import importlib
  RUN_ASYNC_TESTS = True
except ImportError:
  RUN_ASYNC_TESTS = False

try:
  from StringIO import StringIO
except ImportError:
  from io import StringIO

import stem.prereq
import stem.util.conf
import stem.util.log
import stem.util.system
import stem.util.test_tools
import stem.version

import test
import test.arguments
import test.output
import test.runner
import test.task

from test.output import STATUS, SUCCESS, ERROR, NO_NL, STDERR, println

CONFIG = stem.util.conf.config_dict('test', {
  'integ.test_directory': './test/data',
  'test.unit_tests': '',
  'test.integ_tests': '',
})

MOCK_UNAVAILABLE_MSG = """\
To run stem's tests you'll need mock...

https://pypi.python.org/pypi/mock/
"""

MOCK_OUT_OF_DATE_MSG = """\
To run stem's tests you'll need mock. You have version %s, but you need
version 0.8.0 or later...

https://pypi.python.org/pypi/mock/
"""

NEW_CAPABILITIES_FOUND = """\
Your version of Tor has capabilities stem currently isn't taking advantage of.
If you're running the latest version of stem then please file a ticket on:

  https://trac.torproject.org/projects/tor/wiki/doc/stem/bugs

New capabilities are:
"""


def get_unit_tests(module_prefix = None):
  """
  Provides the classes for our unit tests.

  :param str module_prefix: only provide the test if the module starts with
    this substring

  :returns: an **iterator** for our unit tests
  """

  if module_prefix and not module_prefix.startswith('test.unit.'):
    module_prefix = 'test.unit.' + module_prefix

  return _get_tests(CONFIG['test.unit_tests'].splitlines(), module_prefix)


def get_integ_tests(module_prefix = None):
  """
  Provides the classes for our integration tests.

  :param str module_prefix: only provide the test if the module starts with
    this substring

  :returns: an **iterator** for our integration tests
  """

  if module_prefix and not module_prefix.startswith('test.integ.'):
    module_prefix = 'test.integ.' + module_prefix

  return _get_tests(CONFIG['test.integ_tests'].splitlines(), module_prefix)


def _get_tests(modules, module_prefix):
  for import_name in modules:
    module, module_name = import_name.rsplit('.', 1)  # example: util.conf.TestConf

    if not module_prefix or module.startswith(module_prefix):
      yield import_name
    elif module_prefix.startswith(module):
      # single test for this module

      test_name = module_prefix.rsplit('.', 1)[1]
      yield '%s.%s' % (import_name, test_name)


def main():
  start_time = time.time()

  try:
    stem.prereq.check_requirements()
  except ImportError as exc:
    println('%s\n' % exc)
    sys.exit(1)

  test_config = stem.util.conf.get_config('test')
  test_config.load(os.path.join(test.STEM_BASE, 'test', 'settings.cfg'))

  try:
    args = test.arguments.parse(sys.argv[1:])
    test.task.TOR_VERSION.args = (args.tor_path,)
    test.output.SUPPRESS_STDOUT = args.quiet
  except ValueError as exc:
    println(str(exc))
    sys.exit(1)

  if args.print_help:
    println(test.arguments.get_help())
    sys.exit()
  elif not args.run_unit and not args.run_integ:
    println('Nothing to run (for usage provide --help)\n')
    sys.exit()

  if not stem.prereq.is_mock_available():
    try:
      import mock
      println(MOCK_OUT_OF_DATE_MSG % mock.__version__)
    except ImportError:
      println(MOCK_UNAVAILABLE_MSG)

    if stem.util.system.is_available('pip'):
      println("You can get it by running 'sudo pip install mock'.")
    elif stem.util.system.is_available('apt-get'):
      println("You can get it by running 'sudo apt-get install python-mock'.")

    sys.exit(1)

  test.task.run(
    'INITIALISING',
    test.task.STEM_VERSION,
    test.task.TOR_VERSION if args.run_integ else None,
    test.task.PYTHON_VERSION,
    test.task.PLATFORM_VERSION,
    test.task.CRYPTO_VERSION,
    test.task.PYNACL_VERSION,
    test.task.MOCK_VERSION,
    test.task.PYFLAKES_VERSION,
    test.task.PYCODESTYLE_VERSION,
    test.task.CLEAN_PYC,
    test.task.UNUSED_TESTS,
    test.task.IMPORT_TESTS,
    test.task.PYFLAKES_TASK if not args.specific_test else None,
    test.task.PYCODESTYLE_TASK if not args.specific_test else None,
  )

  # buffer that we log messages into so they can be printed after a test has finished

  logging_buffer = stem.util.log.LogBuffer(args.logging_runlevel)
  stem.util.log.get_logger().addHandler(logging_buffer)

  # filters for how testing output is displayed

  error_tracker = test.output.ErrorTracker()

  output_filters = (
    error_tracker.get_filter(),
    test.output.runtimes,
    test.output.strip_module,
    test.output.align_results,
    test.output.colorize,
  )

  # Number of tests that we have skipped. This is only available with python
  # 2.7 or later because before that test results didn't have a 'skipped'
  # attribute.

  skipped_tests = 0

  if args.run_integ:
    default_test_dir = stem.util.system.expand_path(CONFIG['integ.test_directory'], test.STEM_BASE)
    async_args = test.AsyncTestArgs(default_test_dir, args.tor_path)

    for module_str in stem.util.test_tools.ASYNC_TESTS:
      if RUN_ASYNC_TESTS and (not args.specific_test or module_str.startswith(args.specific_test)):
        module = importlib.import_module(module_str.rsplit('.', 1)[0])
        test_classes = [v for k, v in module.__dict__.items() if k.startswith('Test')]

        if len(test_classes) != 1:
          print('BUG: Detected multiple tests for %s: %s' % (module_str, ', '.join(test_classes)))
          sys.exit(1)

        test_classes[0].run_tests(async_args)

  if args.run_unit:
    test.output.print_divider('UNIT TESTS', True)
    error_tracker.set_category('UNIT TEST')

    for test_class in get_unit_tests(args.specific_test):
      run_result = _run_test(args, test_class, output_filters)
      test.output.print_logging(logging_buffer)
      skipped_tests += len(getattr(run_result, 'skipped', []))

    println()

  if args.run_integ:
    test.output.print_divider('INTEGRATION TESTS', True)
    integ_runner = test.runner.get_runner()

    for target in args.run_targets:
      error_tracker.set_category(target)

      try:
        integ_runner.start(target, args.attribute_targets, args.tor_path)

        println('Running tests...\n', STATUS)

        for test_class in get_integ_tests(args.specific_test):
          if integ_runner.assert_tor_is_running():
            run_result = _run_test(args, test_class, output_filters)
            test.output.print_logging(logging_buffer)
            skipped_tests += len(getattr(run_result, 'skipped', []))
          else:
            # our tor process died

            error_tracker.register_error()
            break
      except KeyboardInterrupt:
        println('  aborted starting tor: keyboard interrupt\n', ERROR)
        break
      except ValueError as exc:
        println(str(exc), ERROR)  # can arise if there's bad settings.cfg data
        break
      except OSError:
        error_tracker.register_error()
      finally:
        println()
        integ_runner.stop()
        println()

        # We should have joined on all threads. If not then that indicates a
        # leak that could both likely be a bug and disrupt further targets.

        active_threads = threading.enumerate()

        if len(active_threads) > 1:
          println('Threads lingering after test run:', ERROR)

          for lingering_thread in active_threads:
            println('  %s' % lingering_thread, ERROR)

          break

  static_check_issues = {}

  for task in (test.task.PYFLAKES_TASK, test.task.PYCODESTYLE_TASK):
    if not task.is_available and task.unavailable_msg:
      println(task.unavailable_msg, ERROR)
    else:
      task.join()  # no-op if these haven't been run

      if task.result:
        for path, issues in task.result.items():
          for issue in issues:
            static_check_issues.setdefault(path, []).append(issue)

  _print_static_issues(static_check_issues)

  if error_tracker.has_errors_occured():
    println('TESTING FAILED (%i seconds)' % (time.time() - start_time), ERROR, STDERR)

    for line in error_tracker:
      println('  %s' % line, ERROR, STDERR)

    error_modules = error_tracker.get_modules()

    if len(error_modules) < 10 and not args.specific_test:
      println('\nYou can re-run just these tests with:\n', ERROR, STDERR)

      for module in error_modules:
        println('  %s --test %s' % (' '.join(sys.argv), module), ERROR, STDERR)
  else:
    if skipped_tests > 0:
      println('%i TESTS WERE SKIPPED' % skipped_tests, STATUS)

    println('TESTING PASSED (%i seconds)\n' % (time.time() - start_time), SUCCESS)

  new_capabilities = test.get_new_capabilities()

  if new_capabilities:
    println(NEW_CAPABILITIES_FOUND, ERROR)

    for capability_type, msg in new_capabilities:
      println('  [%s] %s' % (capability_type, msg), ERROR)

  sys.exit(1 if error_tracker.has_errors_occured() else 0)


def _print_static_issues(static_check_issues):
  if static_check_issues:
    println('STATIC CHECKS', STATUS)

    for file_path in static_check_issues:
      println('* %s' % file_path, STATUS)

      # Make a dict of line numbers to its issues. This is so we can both sort
      # by the line number and clear any duplicate messages.

      line_to_issues = {}

      for issue in static_check_issues[file_path]:
        line_to_issues.setdefault(issue.line_number, set()).add((issue.message, issue.line))

      for line_number in sorted(line_to_issues.keys()):
        for msg, line in line_to_issues[line_number]:
          line_count = '%-4s' % line_number
          content = ' | %s' % line.strip() if line.strip() else ''
          println('  line %s - %-40s%s' % (line_count, msg, content))

      println()


def _run_test(args, test_class, output_filters):
  start_time = time.time()

  # Test classes look like...
  #
  #   test.unit.util.conf.TestConf.test_parse_enum_csv
  #
  # We want to strip the 'test.unit.' or 'test.integ.' prefix since it's
  # redundant. We also want to drop the test class name. The individual test
  # name at the end it optional (only present if we used the '--test'
  # argument).

  label_comp = test_class.split('.')[2:]
  del label_comp[-1 if label_comp[-1][0].isupper() else -2]
  test_label = '  %-52s' % ('.'.join(label_comp) + '...')

  if args.verbose:
    test.output.print_divider(test_class)
  else:
    println(test_label, STATUS, NO_NL)

  try:
    suite = unittest.TestLoader().loadTestsFromName(test_class)
  except AttributeError:
    if args.specific_test:
      # should only come up if user provided '--test' for something that doesn't exist
      println(' no such test', ERROR)
      return None
    else:
      raise
  except Exception as exc:
    println(' failed', ERROR)
    traceback.print_exc(exc)
    return None

  test_results = StringIO()
  run_result = stem.util.test_tools.TimedTestRunner(test_results, verbosity = 2).run(suite)

  if args.verbose:
    println(test.output.apply_filters(test_results.getvalue(), *output_filters))
  elif not run_result.failures and not run_result.errors:
    println(' success (%0.2fs)' % (time.time() - start_time), SUCCESS)
  else:
    if args.quiet:
      println(test_label, STATUS, NO_NL, STDERR)
      println(' failed (%0.2fs)' % (time.time() - start_time), ERROR, STDERR)
      println(test.output.apply_filters(test_results.getvalue(), *output_filters), STDERR)
    else:
      println(' failed (%0.2fs)' % (time.time() - start_time), ERROR)
      println(test.output.apply_filters(test_results.getvalue(), *output_filters), NO_NL)

  return run_result


if __name__ == '__main__':
  main()
