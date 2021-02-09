// Names disallowed by NativeIO
const kBadNativeIoNames = [
  "Uppercase",
  "has-dash",
  "has.dot",
  "has/slash",
];

// Returns a handle to a newly created file that holds some data.
//
// The file will be closed and deleted when the test ends.
async function createFile(testCase, fileName) {
  const file = await nativeIO.open(fileName);
  testCase.add_cleanup(async () => {
    await file.close();
    await nativeIO.delete(fileName);
  });

  const writeSharedArrayBuffer = new SharedArrayBuffer(4);
  const writtenBytes = new Uint8Array(writeSharedArrayBuffer);
  writtenBytes.set([64, 65, 66, 67]);
  const writeCount = await file.write(writtenBytes, 0);
  assert_equals(writeCount, 4);

  return file;
}

// Returns a handle to a newly created file that holds some data.
//
// The file will be closed and deleted when the test ends.
function createFileSync(testCase, fileName, data = [64, 65, 66, 67],
  deleteAfter = true) {
  const file = nativeIO.openSync(fileName);
  testCase.add_cleanup(() => {
    file.close();
    if (deleteAfter) {
      nativeIO.deleteSync(fileName);
      nativeIO.releaseCapacitySync(data.length);
    }
  });

  var available_capacity = nativeIO.requestCapacitySync(data.length);
  assert_greater_than_equal(available_capacity, data.length);

  const writtenBytes = Uint8Array.from(data);
  const writeCount = file.write(writtenBytes, 0);
  assert_equals(writeCount, data.length,
    'NativeIOFileSync.write() should resolve with the number of bytes written');

  return file;
}

// Returns an Uint8Array with pseudorandom data.
//
// The PRNG should be sufficient to defeat compression schemes, but it is not
// cryptographically strong.
function createLargeArray(size, seed) {
  const buffer = new Uint8Array(size);

  // 32-bit xorshift - the seed can't be zero
  let state = 1000 + seed;

  for (let i = 0; i < size; ++i) {
    state ^= state << 13;
    state ^= state >> 17;
    state ^= state << 5;
    buffer[i] = state & 0xff;
  }

  return buffer;
}

// Attempts to read the entire file into a buffer.
async function readIoFile(file) {
  const length = await file.getLength();
  const readBuffer = new Uint8Array(new SharedArrayBuffer(length));
  await file.read(readBuffer, 0);
  return readBuffer;
}

// Attempts to read the entire file into a buffer.
function readIoFileSync(file) {
  const length = file.getLength();
  const readBuffer = new Uint8Array(length);
  file.read(readBuffer, 0);
  return readBuffer;
}
