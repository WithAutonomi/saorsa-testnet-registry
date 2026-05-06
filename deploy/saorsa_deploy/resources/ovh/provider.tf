# OVH provider reads OVH_ENDPOINT / OVH_APPLICATION_KEY / OVH_APPLICATION_SECRET
# / OVH_CONSUMER_KEY directly from environment, so no explicit configuration is
# needed here.
provider "ovh" {}

# OpenStack provider is configured from the credentials of the OVH project
# user we create below (see main.tf). OVH's cloud is OpenStack-backed; we use
# this provider for the volume-attach operation, which the OVH provider does
# not yet expose as a native resource.
provider "openstack" {
  auth_url    = "https://auth.cloud.ovh.net/v3/"
  domain_name = "default"
  user_name   = ovh_cloud_project_user.attach_user.username
  password    = ovh_cloud_project_user.attach_user.password
  tenant_id   = var.service_name
  region      = var.region
}
