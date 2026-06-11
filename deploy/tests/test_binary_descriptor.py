from saorsa_deploy.binary_descriptor import build_binary_descriptor


def test_branch_takes_precedence_over_version():
    assert build_binary_descriptor("feature-x", "myorg", "0.3.0") == {
        "kind": "branch",
        "branch": "feature-x",
        "repo_owner": "myorg",
        "debug": False,
    }


def test_branch_debug_recorded():
    d = build_binary_descriptor("feature-x", "myorg", None, debug=True)
    assert d == {
        "kind": "branch",
        "branch": "feature-x",
        "repo_owner": "myorg",
        "debug": True,
    }


def test_release_ignores_debug():
    # debug is only meaningful for branch builds; release stays minimal.
    assert build_binary_descriptor(None, None, "0.3.0", debug=True) == {
        "kind": "release",
        "version": "0.3.0",
    }


def test_branch_defaults_repo_owner_to_maidsafe():
    assert build_binary_descriptor("feature-x", None, None)["repo_owner"] == "maidsafe"


def test_release_version():
    assert build_binary_descriptor(None, None, "0.3.0") == {
        "kind": "release",
        "version": "0.3.0",
    }


def test_no_flags_means_latest():
    assert build_binary_descriptor(None, None, None) == {"kind": "latest"}
