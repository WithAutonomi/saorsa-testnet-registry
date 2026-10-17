"""Unit tests for the prod-downloader log-pruning timer command builder."""

from saorsa_deploy.provisioning.prod_downloader import (
    LOG_DIR,
    LOG_PRUNE_NAME,
    LOG_PRUNE_SERVICE_PATH,
    LOG_PRUNE_TIMER_PATH,
    build_log_prune_command,
)


class TestBuildLogPruneCommand:
    def test_installs_timer_with_retention_window(self):
        cmd = build_log_prune_command(7)
        # The oneshot service deletes files older than N days under LOG_DIR.
        assert f"/usr/bin/find {LOG_DIR} -type f -mtime +7 -delete" in cmd
        # Service + timer unit files are written and the timer enabled.
        assert LOG_PRUNE_SERVICE_PATH in cmd
        assert LOG_PRUNE_TIMER_PATH in cmd
        assert f"systemctl enable --now {LOG_PRUNE_NAME}.timer" in cmd
        assert "OnCalendar=daily" in cmd

    def test_retention_window_reflects_value(self):
        assert "-mtime +3 -delete" in build_log_prune_command(3)
        assert "-mtime +30 -delete" in build_log_prune_command(30)

    def test_zero_disables_and_removes_timer(self):
        cmd = build_log_prune_command(0)
        assert f"systemctl disable --now {LOG_PRUNE_NAME}.timer" in cmd
        assert f"rm -f {LOG_PRUNE_TIMER_PATH} {LOG_PRUNE_SERVICE_PATH}" in cmd
        # No find/install when pruning is disabled.
        assert "find" not in cmd
        assert "enable --now" not in cmd

    def test_negative_treated_as_disabled(self):
        assert "disable --now" in build_log_prune_command(-1)


class TestProvisionerThreadsRetention:
    def test_default_retention_is_seven(self):
        from saorsa_deploy.provisioning.prod_downloader import (
            ProdDownloaderProvisioner,
        )

        prov = ProdDownloaderProvisioner(
            downloaders=[],
            bootstrap_peers=[],
        )
        assert prov.log_retention_days == 7

    def test_retention_override_stored(self):
        from saorsa_deploy.provisioning.prod_downloader import (
            ProdDownloaderProvisioner,
        )

        prov = ProdDownloaderProvisioner(
            downloaders=[],
            bootstrap_peers=[],
            log_retention_days=0,
        )
        assert prov.log_retention_days == 0
