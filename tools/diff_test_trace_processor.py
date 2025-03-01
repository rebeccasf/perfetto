#!/usr/bin/env python3
# Copyright (C) 2018 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import concurrent.futures
import datetime
import difflib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile

from google.protobuf import text_format

from proto_utils import create_message_factory, serialize_textproto_trace, serialize_python_trace

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = {
    'PERFETTO_BINARY_PATH': os.path.join(ROOT_DIR, 'test', 'data'),
}
if sys.platform.startswith('linux'):
  ENV['PATH'] = os.path.join(ROOT_DIR, 'buildtools', 'linux64', 'clang', 'bin')
elif sys.platform.startswith('darwin'):
  # Sadly, on macOS we need to check out the Android deps to get
  # llvm symbolizer.
  ENV['PATH'] = os.path.join(ROOT_DIR, 'buildtools', 'ndk', 'toolchains',
                             'llvm', 'prebuilt', 'darwin-x86_64', 'bin')
elif sys.platform.startswith('win32'):
  ENV['PATH'] = os.path.join(ROOT_DIR, 'buildtools', 'win', 'clang', 'bin')

USE_COLOR_CODES = sys.stderr.isatty()

def red(no_colors):
  return "\u001b[31m" if USE_COLOR_CODES and not no_colors else ""


def green(no_colors):
  return "\u001b[32m" if USE_COLOR_CODES and not no_colors else ""


def yellow(no_colors):
  return "\u001b[33m" if USE_COLOR_CODES and not no_colors else ""


def end_color(no_colors):
  return "\u001b[0m" if USE_COLOR_CODES and not no_colors else ""


class Test(object):

  def __init__(self, type, trace_path, query_path_or_metric, expected_path):
    self.type = type
    self.trace_path = trace_path
    self.query_path_or_metric = query_path_or_metric
    self.expected_path = expected_path


class PerfResult(object):

  def __init__(self, test_type, trace_path, query_path_or_metric,
               ingest_time_ns_str, real_time_ns_str):
    self.test_type = test_type
    self.trace_path = trace_path
    self.query_path_or_metric = query_path_or_metric
    self.ingest_time_ns = int(ingest_time_ns_str)
    self.real_time_ns = int(real_time_ns_str)


class TestResult(object):

  def __init__(self, test_type, input_name, trace, cmd, expected, actual,
               stderr, exit_code):
    self.test_type = test_type
    self.input_name = input_name
    self.trace = trace
    self.cmd = cmd
    self.expected = expected
    self.actual = actual
    self.stderr = stderr
    self.exit_code = exit_code


def create_metrics_message_factory(metrics_descriptor_paths):
  return create_message_factory(metrics_descriptor_paths,
                                'perfetto.protos.TraceMetrics')


def write_diff(expected, actual):
  expected_lines = expected.splitlines(True)
  actual_lines = actual.splitlines(True)
  diff = difflib.unified_diff(
      expected_lines, actual_lines, fromfile='expected', tofile='actual')
  res = ""
  for line in diff:
    res += line
  return res


def run_metrics_test(trace_processor_path, gen_trace_path, metric,
                     expected_path, perf_path, metrics_message_factory):
  with open(expected_path, 'r') as expected_file:
    expected = expected_file.read()

  json_output = os.path.basename(expected_path).endswith('.json.out')
  cmd = [
      trace_processor_path,
      '--run-metrics',
      metric,
      '--metrics-output=%s' % ('json' if json_output else 'binary'),
      '--perf-file',
      perf_path,
      gen_trace_path,
  ]
  tp = subprocess.Popen(
      cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV)
  (stdout, stderr) = tp.communicate()

  if json_output:
    expected_text = expected
    actual_text = stdout.decode('utf8')
  else:
    # Expected will be in text proto format and we'll need to parse it to
    # a real proto.
    expected_message = metrics_message_factory()
    text_format.Merge(expected, expected_message)

    # Actual will be the raw bytes of the proto and we'll need to parse it
    # into a message.
    actual_message = metrics_message_factory()
    actual_message.ParseFromString(stdout)

    # Convert both back to text format.
    expected_text = text_format.MessageToString(expected_message)
    actual_text = text_format.MessageToString(actual_message)

  return TestResult('metric', metric, gen_trace_path, cmd, expected_text,
                    actual_text, stderr.decode('utf8'), tp.returncode)


def run_query_test(trace_processor_path, gen_trace_path, query_path,
                   expected_path, perf_path):
  with open(expected_path, 'r') as expected_file:
    expected = expected_file.read()

  cmd = [
      trace_processor_path,
      '-q',
      query_path,
      '--perf-file',
      perf_path,
      gen_trace_path,
  ]
  tp = subprocess.Popen(
      cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV)
  (stdout, stderr) = tp.communicate()
  return TestResult('query', query_path, gen_trace_path, cmd, expected,
                    stdout.decode('utf8'), stderr.decode('utf8'), tp.returncode)


def run_test(trace_descriptor_path, extension_descriptor_paths, args, test):
  """
  Returns:
    test_name -> str,
    passed -> bools,
    result_str -> str,
    perf_data -> str
  """
  out_path = os.path.dirname(args.trace_processor)
  if args.metrics_descriptor:
    metrics_descriptor_paths = [args.metrics_descriptor]
  else:
    metrics_protos_path = os.path.join(out_path, 'gen', 'protos', 'perfetto',
                                       'metrics')
    metrics_descriptor_paths = [
        os.path.join(metrics_protos_path, 'metrics.descriptor'),
        os.path.join(metrics_protos_path, 'chrome',
                     'all_chrome_metrics.descriptor')
    ]
  metrics_message_factory = create_message_factory(
      metrics_descriptor_paths, 'perfetto.protos.TraceMetrics')
  result_str = ""
  red_str = red(args.no_colors)
  green_str = green(args.no_colors)
  end_color_str = end_color(args.no_colors)
  trace_path = test.trace_path
  expected_path = test.expected_path
  test_name = f"{os.path.basename(test.query_path_or_metric)}\
  {os.path.basename(trace_path)}"

  if not os.path.exists(trace_path):
    result_str += f"Trace file not found {trace_path}\n"
    return test_name, False, result_str, ""
  elif not os.path.exists(expected_path):
    result_str = f"Expected file not found {expected_path}"
    return test_name, False, result_str, ""

  is_generated_trace = trace_path.endswith('.py') or trace_path.endswith(
      '.textproto')
  if trace_path.endswith('.py'):
    gen_trace_file = tempfile.NamedTemporaryFile(delete=False)
    serialize_python_trace(trace_descriptor_path, trace_path, gen_trace_file)
    gen_trace_path = os.path.realpath(gen_trace_file.name)
  elif trace_path.endswith('.textproto'):
    gen_trace_file = tempfile.NamedTemporaryFile(delete=False)
    serialize_textproto_trace(trace_descriptor_path, extension_descriptor_paths,
                              trace_path, gen_trace_file)
    gen_trace_path = os.path.realpath(gen_trace_file.name)
  else:
    gen_trace_file = None
    gen_trace_path = trace_path

  # We can't use delete=True here. When using that on Windows, the
  # resulting file is opened in exclusive mode (in turn that's a subtle
  # side-effect of the underlying CreateFile(FILE_ATTRIBUTE_TEMPORARY))
  # and TP fails to open the passed path.
  tmp_perf_file = tempfile.NamedTemporaryFile(delete=False)
  result_str += f"{yellow(args.no_colors)}[ RUN      ]{end_color_str} "
  result_str += f"{test_name}\n"

  tmp_perf_path = tmp_perf_file.name
  if test.type == 'queries':
    query_path = test.query_path_or_metric

    if not os.path.exists(test.query_path_or_metric):
      result_str += f"Query file not found {query_path}"
      return test_name, False, result_str, ""

    result = run_query_test(args.trace_processor, gen_trace_path, query_path,
                            expected_path, tmp_perf_path)
  elif test.type == 'metrics':
    result = run_metrics_test(args.trace_processor, gen_trace_path,
                              test.query_path_or_metric, expected_path,
                              tmp_perf_path, metrics_message_factory)
  else:
    assert False

  perf_lines = [line.decode('utf8') for line in tmp_perf_file.readlines()]
  tmp_perf_file.close()
  os.remove(tmp_perf_file.name)

  if gen_trace_file:
    if args.keep_input:
      result_str += f"Saving generated input trace: {gen_trace_path}\n"
    else:
      gen_trace_file.close()
      os.remove(gen_trace_path)

  def write_cmdlines():
    res = ""
    if is_generated_trace:
      res += 'Command to generate trace:\n'
      res += 'tools/serialize_test_trace.py '
      res += '--descriptor {} {} > {}\n'.format(
          os.path.relpath(trace_descriptor_path, ROOT_DIR),
          os.path.relpath(trace_path, ROOT_DIR),
          os.path.relpath(gen_trace_path, ROOT_DIR))
    res += f"Command line:\n{' '.join(result.cmd)}\n"
    return res

  expected_content = result.expected.replace('\r\n', '\n')
  actual_content = result.actual.replace('\r\n', '\n')
  contents_equal = (expected_content == actual_content)
  if result.exit_code != 0 or not contents_equal:
    result_str += result.stderr

    if result.exit_code == 0:
      result_str += f"Expected did not match actual for trace "
      result_str += f"{trace_path} and {result.test_type} {result.input_name}\n"
      result_str += f"Expected file: {expected_path}\n"
      result_str += write_cmdlines()
      result_str += write_diff(result.expected, result.actual)
    else:
      result_str += write_cmdlines()

    result_str += f"{red_str}[     FAIL ]{end_color_str} {test_name} "
    result_str += f"{os.path.basename(trace_path)}\n"

    if args.rebase:
      if result.exit_code == 0:
        result_str += f"Rebasing {expected_path}\n"
        with open(expected_path, 'w') as f:
          f.write(result.actual)
        rebased += 1
      else:
        result_str += f"Rebase failed for {expected_path} as query failed\n"

    return test_name, False, result_str, ""
  else:
    assert len(perf_lines) == 1
    perf_numbers = perf_lines[0].split(',')

    assert len(perf_numbers) == 2
    perf_result = PerfResult(test.type, trace_path, test.query_path_or_metric,
                             perf_numbers[0], perf_numbers[1])

    result_str += f"{green_str}[       OK ]{end_color_str} "
    result_str += f"{os.path.basename(test.query_path_or_metric)} "
    result_str += f"{os.path.basename(trace_path)} "
    result_str += f"(ingest: {perf_result.ingest_time_ns / 1000000:.2f} ms "
    result_str += f"query: {perf_result.real_time_ns / 1000000:.2f} ms)\n"
  return test_name, True, result_str, perf_result


def run_all_tests(trace_descriptor_path, extension_descriptor_paths, args,
                  tests):
  perf_data = []
  test_failure = []
  rebased = 0
  with concurrent.futures.ProcessPoolExecutor() as e:
    fut = [
        e.submit(run_test, trace_descriptor_path, extension_descriptor_paths,
                 args, test) for test in tests
    ]
    for res in concurrent.futures.as_completed(fut):
      test_name, test_passed, res_str, perf_result = res.result()
      sys.stderr.write(res_str)
      if test_passed:
        perf_data.append(perf_result)
        if args.rebase:
          rebased += 1
      else:
        test_failure.append(test_name)

  return test_failure, perf_data, rebased


def read_all_tests_from_index(index_path, query_metric_pattern, trace_pattern):
  index_dir = os.path.dirname(index_path)

  with open(index_path, 'r') as index_file:
    index_lines = index_file.readlines()

  tests = []
  for line in index_lines:
    stripped = line.strip()
    if stripped.startswith('#'):
      continue
    elif not stripped:
      continue

    [trace_fname, query_fname_or_metric, expected_fname] = stripped.split(' ')
    if not query_metric_pattern.match(os.path.basename(query_fname_or_metric)):
      continue

    if not trace_pattern.match(os.path.basename(trace_fname)):
      continue

    trace_path = os.path.abspath(os.path.join(index_dir, trace_fname))
    expected_path = os.path.abspath(os.path.join(index_dir, expected_fname))

    if query_fname_or_metric.endswith('.sql'):
      test_type = 'queries'
      query_path_or_metric = os.path.abspath(
          os.path.join(index_dir, query_fname_or_metric))
    else:
      test_type = 'metrics'
      query_path_or_metric = query_fname_or_metric

    tests.append(
        Test(test_type, trace_path, query_path_or_metric, expected_path))
  return tests


def read_all_tests(query_metric_pattern, trace_pattern):
  include_index_dir = os.path.join(ROOT_DIR, 'test', 'trace_processor')
  include_index = os.path.join(include_index_dir, 'include_index')
  tests = []
  with open(include_index, 'r') as include_file:
    for index_relpath in include_file.readlines():
      index_path = os.path.join(include_index_dir, index_relpath.strip())
      tests.extend(
          read_all_tests_from_index(index_path, query_metric_pattern,
                                    trace_pattern))
  return tests


def ctrl_c_handler(_num, _frame):
  # Send a sigkill to the whole process group. Our process group looks like:
  # - Main python interpreter running the main()
  #   - N python interpreters coming from ProcessPoolExecutor workers.
  #     - 1 trace_processor_shell subprocess coming from the subprocess.Popen().
  # We don't need any graceful termination as the diff tests are stateless and
  # don't write any file. Just kill them all immediately.
  os.killpg(os.getpid(), signal.SIGKILL)


def main():
  signal.signal(signal.SIGINT, ctrl_c_handler)
  parser = argparse.ArgumentParser()
  parser.add_argument('--test-type', type=str, default='all')
  parser.add_argument('--trace-descriptor', type=str)
  parser.add_argument('--metrics-descriptor', type=str)
  parser.add_argument('--perf-file', type=str)
  parser.add_argument(
      '--query-metric-filter',
      default='.*',
      type=str,
      help='Filter the name of query files or metrics to test (regex syntax)')
  parser.add_argument(
      '--trace-filter',
      default='.*',
      type=str,
      help='Filter the name of trace files to test (regex syntax)')
  parser.add_argument(
      '--keep-input',
      action='store_true',
      help='Save the (generated) input pb file for debugging')
  parser.add_argument(
      '--rebase',
      action='store_true',
      help='Update the expected output file with the actual result')
  parser.add_argument(
      '--no-colors', action='store_true', help='Print without coloring')
  parser.add_argument(
      'trace_processor', type=str, help='location of trace processor binary')
  args = parser.parse_args()

  query_metric_pattern = re.compile(args.query_metric_filter)
  trace_pattern = re.compile(args.trace_filter)

  tests = read_all_tests(query_metric_pattern, trace_pattern)
  sys.stderr.write(f"[==========] Running {len(tests)} tests.\n")

  out_path = os.path.dirname(args.trace_processor)
  if args.trace_descriptor:
    trace_descriptor_path = args.trace_descriptor
  else:

    def find_trace_descriptor(parent):
      trace_protos_path = os.path.join(parent, 'gen', 'protos', 'perfetto',
                                       'trace')
      return os.path.join(trace_protos_path, 'trace.descriptor')

    trace_descriptor_path = find_trace_descriptor(out_path)
    if not os.path.exists(trace_descriptor_path):
      trace_descriptor_path = find_trace_descriptor(
          os.path.join(out_path, 'gcc_like_host'))

  chrome_extensions = os.path.join(out_path, 'gen', 'protos', 'third_party',
                                   'chromium', 'chrome_track_event.descriptor')
  test_extensions = os.path.join(out_path, 'gen', 'protos', 'perfetto', 'trace',
                                 'test_extensions.descriptor')

  test_run_start = datetime.datetime.now()
  test_failures, perf_data, rebased = run_all_tests(
      trace_descriptor_path, [chrome_extensions, test_extensions], args, tests)
  test_run_end = datetime.datetime.now()
  test_time_ms = int((test_run_end - test_run_start).total_seconds() * 1000)

  sys.stderr.write(
      f"[==========] {len(tests)} tests ran. ({test_time_ms} ms total)\n")
  if test_failures:
    sys.stderr.write(
        f"{red(args.no_colors)}[  PASSED  ]{end_color(args.no_colors)} "
        f"{len(tests) - len(test_failures)} tests.\n")
  else:
    sys.stderr.write(
        f"{green(args.no_colors)}[  PASSED  ]{end_color(args.no_colors)} "
        f"{len(tests)} tests.\n")

  if args.rebase:
    sys.stderr.write('\n')
    sys.stderr.write(f"{rebased} tests rebased.\n")

  if len(test_failures) == 0:
    if args.perf_file:
      test_dir = os.path.join(ROOT_DIR, 'test')
      trace_processor_dir = os.path.join(test_dir, 'trace_processor')

      metrics = []
      sorted_data = sorted(
          perf_data,
          key=lambda x: (x.test_type, x.trace_path, x.query_path_or_metric))
      for perf_args in sorted_data:
        trace_short_path = os.path.relpath(perf_args.trace_path, test_dir)

        query_short_path_or_metric = perf_args.query_path_or_metric
        if perf_args.test_type == 'queries':
          query_short_path_or_metric = os.path.relpath(
              perf_args.query_path_or_metric, trace_processor_dir)

        metrics.append({
            'metric': 'tp_perf_test_ingest_time',
            'value': float(perf_args.ingest_time_ns) / 1.0e9,
            'unit': 's',
            'tags': {
                'test_name': f"{trace_short_path}-{query_short_path_or_metric}",
                'test_type': perf_args.test_type,
            },
            'labels': {},
        })
        metrics.append({
            'metric': 'perf_test_real_time',
            'value': float(perf_args.real_time_ns) / 1.0e9,
            'unit': 's',
            'tags': {
                'test_name': f"{trace_short_path}-{query_short_path_or_metric}",
                'test_type': perf_args.test_type,
            },
            'labels': {},
        })

      output_data = {'metrics': metrics}
      with open(args.perf_file, 'w+') as perf_file:
        perf_file.write(json.dumps(output_data, indent=2))
    return 0
  else:
    for failure in test_failures:
      sys.stderr.write(f"[  FAILED  ] {failure}\n")
    return 1


if __name__ == '__main__':
  sys.exit(main())
