// META: title=Synchronous NativeIO API: Written bytes are read back.
// META: global=dedicatedworker
// META: script=resources/support.js

'use strict';

test(testCase => {
  const writtenBytes = [64, 65, 66, 67];
  const file = createFileSync(testCase, 'test_file', writtenBytes);

  const readBytes = new Uint8Array(writtenBytes.length);
  const readCount = file.read(readBytes, 0);
  assert_equals(readCount, 4,
    'NativeIOFile.read() should return the number of bytes read');

  assert_array_equals(readBytes, writtenBytes,
    'the bytes read should match the bytes written');
}, 'NativeIOFileSync.read returns bytes written by NativeIOFileSync.write');
