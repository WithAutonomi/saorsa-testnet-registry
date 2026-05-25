# OVH provider reads OVH_ENDPOINT / OVH_APPLICATION_KEY / OVH_APPLICATION_SECRET
# / OVH_CONSUMER_KEY directly from the environment, so no explicit
# configuration is needed here. Unlike the node manifest, client VMs have no
# attached volume, so the OpenStack provider (used there only for volume
# attach) is not required.
provider "ovh" {}
