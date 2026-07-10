# Node module for OVH 3-AZ regions (EU-WEST-PAR, EU-SOUTH-MIL).
#
# WHY A SEPARATE MODULE from resources/ovh: OVH's native instance-create API
# (`POST /cloud/project/.../region/{region}/instance`, used by the
# ovh_cloud_project_instance resource) returns HTTP 500 on 3-AZ regions whenever
# the request carries `userData` OR `availabilityZone` — both documented-valid
# optional fields (verified 2026-07-10). The ovh module depends on `userData`
# (cloud-init that enables root SSH), so it cannot target 3-AZ regions. This
# module creates the instance through the OpenStack/Nova API instead
# (openstack_compute_instance_v2), which handles user_data + availability_zone
# correctly and sidesteps the OVH-API bug. The 1-AZ resources/ovh module is left
# untouched so production OVH nodes carry zero risk.

# OVH provider reads OVH_ENDPOINT / OVH_APPLICATION_KEY / OVH_APPLICATION_SECRET
# / OVH_CONSUMER_KEY from the environment.
provider "ovh" {}

# OpenStack provider is configured from the credentials of the OVH project user
# minted in main.tf. OVH's cloud is OpenStack-backed; here we use it for the
# instance itself (not just the volume attach) to avoid the OVH-API 500.
provider "openstack" {
  auth_url    = "https://auth.cloud.ovh.net/v3/"
  domain_name = "default"
  user_name   = ovh_cloud_project_user.attach_user.username
  password    = ovh_cloud_project_user.attach_user.password
  tenant_id   = var.service_name
  region      = var.region
}
