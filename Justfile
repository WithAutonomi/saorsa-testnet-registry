deployment := "ant-prod-01"
ssh_key    := "~/.ssh/ant_deploy"
inventory  := "~/.autonomi/testnets/" + deployment + "/inventory.json"

init-upgrade-tracking from_version to_version:
    uv run saorsa-deploy elastic infra \
        --name {{deployment}} \
        --region lon1 \
        --droplet-size s-8vcpu-16gb \
        --volume-size 500
    uv run saorsa-deploy elastic provision \
        --name {{deployment}} \
        --ssh-key-path {{ssh_key}} \
        --shards 8 \
        --retention-days 7
    uv run saorsa-deploy elastic provision-telegraf \
        --name {{deployment}} \
        --inventory-path {{inventory}} \
        --ssh-key-path {{ssh_key}} \
        --only-new-logs \
        --node-version {{from_version}}
    uv run saorsa-deploy track-upgrades init \
        --name {{deployment}} \
        --inventory-path {{inventory}} \
        --from-version {{from_version}} \
        --to-version {{to_version}}
