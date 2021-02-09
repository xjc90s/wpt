// META: title=NativeIO API: Acquiring, displaying and releasing capacity.
// META: global=dedicatedworker

'use strict';

test(testCase => {
  var available_capacity = nativeIO.getRemainingCapacitySync();
  testCase.add_cleanup(() => {
    nativeIO.releaseCapacitySync(2);
  });
  assert_equals(
    available_capacity, 0,
    'A execution context should starts with no capacity.');
  const requested_capacity = 4;
  available_capacity = nativeIO.requestCapacitySync(requested_capacity);
  assert_equals(
    available_capacity, requested_capacity,
    'nativeIO.requestCapacitySync should grant the requested capacity.');
  const released_capacity = 2;
  available_capacity = nativeIO.releaseCapacitySync(released_capacity);
  assert_equals(
    available_capacity, requested_capacity - released_capacity,
    'nativeIO.releaseCapacitySync() should release the specified capacity.');
}, 'NativeIOFileManager.getRemainingCapacitySync() reports available capacity');

test(testCase => {
  const file = nativeIO.openSync('test_file');
  testCase.add_cleanup(() => {
    file.close();
    nativeIO.deleteSync('test_file');
    nativeIO.releaseCapacitySync(4);
  });

  assert_throws_dom('QuotaExceededError', () => file.setLength(4));

  let available_capacity = nativeIO.requestCapacitySync(2);
  assert_equals(
    available_capacity, 2,
    'nativeIO.requestCapacitySync should grant the requested capacity.');

  assert_throws_dom('QuotaExceededError', () => file.setLength(4));

  available_capacity = nativeIO.requestCapacitySync(2);
  assert_equals(
    available_capacity, 4,
    'nativeIO.requestCapacitySync should grant the requested capacity again.');
  file.setLength(4);
}, 'NativeIOFileSync.setLength() fails if insufficient capacity is allocated.');


test(testCase => {
  const file = nativeIO.openSync('test_file');
  testCase.add_cleanup(() => {
    file.close();
    nativeIO.deleteSync('test_file');
    nativeIO.releaseCapacitySync(4);
  });
  const writtenBytes = Uint8Array.from([64, 65, 66, 67]);

  assert_throws_dom('QuotaExceededError', () => file.write(writtenBytes, 0));

  let available_capacity = nativeIO.requestCapacitySync(2);
  assert_equals(
    available_capacity, 2,
    'nativeIO.requestCapacitySync should grant the requested capacity.');

  assert_throws_dom('QuotaExceededError', () => file.write(writtenBytes, 0));

  available_capacity = nativeIO.requestCapacitySync(2);
  assert_equals(
    available_capacity, 4,
    'nativeIO.requestCapacitySync should grant the requested capacity again.');
  file.write(writtenBytes, 0);
}, 'NativeIOFileSync.write() fails if insufficient capacity is allocated.');
