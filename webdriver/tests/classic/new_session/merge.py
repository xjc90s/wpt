# META: timeout=long

import pytest

from tests.support.asserts import assert_error, assert_success


@pytest.mark.parametrize("body", [
    lambda key, value: {"alwaysMatch": {key: value}},
    lambda key, value: {"firstMatch": [{key: value}]}
], ids=["alwaysMatch", "firstMatch"])
def test_platform_name(new_session, add_browser_capabilities, body, target_platform):
    capabilities = body("platformName", target_platform)
    if "alwaysMatch" in capabilities:
        capabilities["alwaysMatch"] = add_browser_capabilities(capabilities["alwaysMatch"])
    else:
        capabilities["firstMatch"][0] = add_browser_capabilities(capabilities["firstMatch"][0])

    response, _ = new_session({"capabilities": capabilities})
    value = assert_success(response)

    assert value["capabilities"]["platformName"] == target_platform


invalid_merge = [
    ("acceptInsecureCerts", (True, True)),
    ("unhandledPromptBehavior", ("accept", "accept")),
    ("unhandledPromptBehavior", ("accept", "dismiss")),
    ("timeouts", ({"script": 10}, {"script": 10})),
    ("timeouts", ({"script": 10}, {"pageLoad": 10})),
]


@pytest.mark.parametrize("key,value", invalid_merge)
def test_merge_invalid(new_session, add_browser_capabilities, key, value):
    response, _ = new_session({"capabilities": {
        "alwaysMatch": add_browser_capabilities({key: value[0]}),
        "firstMatch": [{}, {key: value[1]}],
    }})
    assert_error(response, "invalid argument")


def test_merge_platform_name(new_session, add_browser_capabilities, target_platform):
    response, _ = new_session({"capabilities": {
        "alwaysMatch": add_browser_capabilities({"timeouts": {"script": 10}}),
        "firstMatch": [{
            "platformName": target_platform.upper(),
            "pageLoadStrategy": "none",
        }, {
            "platformName": target_platform,
            "pageLoadStrategy": "eager",
        }]}})

    value = assert_success(response)

    assert value["capabilities"]["platformName"] == target_platform
    assert value["capabilities"]["pageLoadStrategy"] == "eager"


def test_merge_browserName(new_session, add_browser_capabilities):
    response, _ = new_session(
        {"capabilities": {"alwaysMatch": add_browser_capabilities({})}})
    value = assert_success(response)

    browser_settings = {
        "browserName": value["capabilities"]["browserName"],
        "browserVersion": value["capabilities"]["browserVersion"],
    }

    response, _ = new_session({"capabilities": {
        "alwaysMatch": add_browser_capabilities({"timeouts": {"script": 10}}),
        "firstMatch": [{
            "browserName": browser_settings["browserName"] + "invalid",
            "pageLoadStrategy": "none",
        }, {
            "browserName": browser_settings["browserName"],
            "pageLoadStrategy": "eager",
        }]}}, delete_existing_session=True)
    value = assert_success(response)

    assert value["capabilities"]["browserName"] == browser_settings['browserName']
    assert value["capabilities"]["pageLoadStrategy"] == "eager"
