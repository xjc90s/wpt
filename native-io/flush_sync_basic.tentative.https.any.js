// META: title=Synchronous NativeIO API: Flushed data is read back.
// META: global=dedicatedworker
// META: script=resources/support.js

'use strict';

test(testCase => {
  const size = 1024;
  const longarray = createLargeArray(size, /*seed = */ 107);

  const file = createFileSync(testCase, 'test_file', longarray);

  file.flush();
  const readBytes = readIoFileSync(file);

  assert_array_equals(readBytes, longarray,
    'the bytes read should match the bytes written');
}, 'NativeIOFileSync.read returns bytes written by NativeIOFileSync.write' +
' after NativeIOFileSync.flush');
