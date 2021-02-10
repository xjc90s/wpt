import base64

def main(request, response):
    origin = request.headers.get(b"origin")
    if origin is not None:
        response.headers.set(b"Access-Control-Allow-Origin", origin)
        response.headers.set(b"Access-Control-Allow-Methods", b"GET")
        response.headers.set(b"Access-Control-Allow-Credentials", b"true")

    headers = [(b"Content-Type", b"application/webbundle")]
    milk = request.cookies.first(b"milk", None)

    if (milk is not None) and milk.value == b"yes":
        # We embed a web bundle as a base64 encoded string, instead of reading
        # an external file from a WPT's python handler. We can reproduce this
        # string as follows:

        # import base64
        # with open("../resources/wbn/subresource.wbn") as f:
        #   |p|r|i|n|t|(base64.b64encode(f.read()))  # |p|r|i|n|t| suppresses a wpt lint error.
        bundle = base64.b64decode("hkjwn4yQ8J+TpkRiMQAAeD5odHRwOi8vd2ViLXBsYXRmb3JtLnRlc3Q6ODAwMS93ZWItYnVuZGxlL3Jlc291cmNlcy93Ym4vcm9vdC5qc1aEZWluZGV4GJFpcmVzcG9uc2VzGQFPgqJ4Pmh0dHA6Ly93ZWItcGxhdGZvcm0udGVzdDo4MDAxL3dlYi1idW5kbGUvcmVzb3VyY2VzL3dibi9yb290Lmpzg0ABGKl4Q2h0dHA6Ly93ZWItcGxhdGZvcm0udGVzdDo4MDAxL3dlYi1idW5kbGUvcmVzb3VyY2VzL3dibi9zdWJtb2R1bGUuanODQBiqGKWCgliEpUc6c3RhdHVzQzIwMExjb250ZW50LXR5cGVWYXBwbGljYXRpb24vamF2YXNjcmlwdE1hY2NlcHQtcmFuZ2VzRWJ5dGVzTWxhc3QtbW9kaWZpZWRYHVR1ZSwgMjEgSnVsIDIwMjAgMDQ6NDI6NTYgR01UTmNvbnRlbnQtbGVuZ3RoQjMyWCBleHBvcnQgKiBmcm9tICcuL3N1Ym1vZHVsZS5qcyc7CoJYhKVHOnN0YXR1c0MyMDBMY29udGVudC10eXBlVmFwcGxpY2F0aW9uL2phdmFzY3JpcHRNYWNjZXB0LXJhbmdlc0VieXRlc01sYXN0LW1vZGlmaWVkWB1UdWUsIDIxIEp1bCAyMDIwIDA0OjQyOjU2IEdNVE5jb250ZW50LWxlbmd0aEIyOFgcZXhwb3J0IGNvbnN0IHJlc3VsdCA9ICdPSyc7CkgAAAAAAAACUA==")
        return (200, headers, bundle)
    else:
        return (400, headers, '')
