const directory = '/html/cross-origin-embedder-policy/credentialless';

const executor_path = directory + '/resources/executor.html?pipe=';
const same_origin = get_host_info().HTTPS_ORIGIN;
const cross_origin = get_host_info().HTTPS_REMOTE_ORIGIN;
const coep_none = '|header(Cross-Origin-Embedder-Policy,none)';
const coep_credentialless =
    '|header(Cross-Origin-Embedder-Policy,credentialless)';

// Test using the modern async/await primitives are easier to read/write.
// However they run sequentially, contrary to async_test. This is the parallel
// version, to avoid timing out.
let promise_test_parallel = (promise, description) => {
  async_test(test => {
    promise(test)
      .then(() => test.done())
      .catch(test.step_func(error => { throw error; }));
  }, description);
};
