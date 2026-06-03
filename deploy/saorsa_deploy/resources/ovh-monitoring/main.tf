# Look up the InfluxDB flavor (e.g. b3-8) by name in the target region; the
# OVH instance resource needs a flavor_id, not a name.
data "ovh_cloud_project_flavors" "influxdb" {
  service_name = var.service_name
  region       = var.region
  name_filter  = var.flavor_name
}

# Look up the (small) Grafana flavor by name in the target region.
data "ovh_cloud_project_flavors" "grafana" {
  service_name = var.service_name
  region       = var.region
  name_filter  = var.grafana_flavor_name
}

# Look up Linux images in the region; pick the latest matching the chosen
# Ubuntu name pattern.
data "ovh_cloud_project_images" "linux" {
  service_name = var.service_name
  region       = var.region
  os_type      = "linux"
}

locals {
  influxdb_flavor_id = one(data.ovh_cloud_project_flavors.influxdb.flavors).id
  grafana_flavor_id  = one(data.ovh_cloud_project_flavors.grafana.flavors).id

  ubuntu_images = [
    for img in data.ovh_cloud_project_images.linux.images :
    img if can(regex(var.ubuntu_image_name_pattern, img.name))
  ]
  ubuntu_image_id = local.ubuntu_images[0].id

  # OVH's Ubuntu image disables root SSH and uses the 'ubuntu' cloud-init
  # user. The other providers (DO, Hetzner, Vultr) all allow root SSH on
  # their cloud images, and the monitoring provisioner connects as root, so
  # enable root SSH on first boot to match.
  enable_root_ssh_user_data = <<-EOT
    #cloud-config
    disable_root: false
    runcmd:
      - install -d -m 700 -o root -g root /root/.ssh
      - cp /home/ubuntu/.ssh/authorized_keys /root/.ssh/authorized_keys
      - chown root:root /root/.ssh/authorized_keys
      - chmod 600 /root/.ssh/authorized_keys
      - sed -i 's|^#\?PermitRootLogin .*|PermitRootLogin prohibit-password|' /etc/ssh/sshd_config
      - systemctl reload sshd
  EOT
}

# OpenStack-compatible user used by the openstack provider for the InfluxDB
# data-volume attach. compute_operator + volume_operator give us instance +
# volume + attachment permissions. Project-scoped; the password is generated
# by OVH and only retrievable on creation, so it lives in Terraform state.
resource "ovh_cloud_project_user" "attach_user" {
  service_name = var.service_name
  description  = "saorsa-deploy: openstack auth for ${var.name}-monitoring-${var.region}"
  role_names   = ["compute_operator", "volume_operator"]
}

# InfluxDB VM (with attached data volume).
resource "ovh_cloud_project_instance" "monitoring" {
  service_name   = var.service_name
  region         = var.region
  billing_period = "hourly"
  name           = "${var.name}-ant-monitoring-${var.region}"
  user_data      = local.enable_root_ssh_user_data

  boot_from {
    image_id = local.ubuntu_image_id
  }

  flavor {
    flavor_id = local.influxdb_flavor_id
  }

  ssh_key {
    name = var.ssh_key_name
  }

  network {
    public = true
  }
}

resource "ovh_cloud_project_volume" "monitoring_storage" {
  service_name = var.service_name
  region_name  = var.region
  size         = var.attached_volume_size
  type         = var.volume_type
  name         = "${lower(var.name)}-ant-monitoring-storage-${var.region}"
  description  = "InfluxDB data volume for ${ovh_cloud_project_instance.monitoring.name}"
}

# OVH's Terraform provider has no native volume-attach resource, so we go
# through the OpenStack provider (Cinder volume attach) using credentials
# minted by ovh_cloud_project_user above.
resource "openstack_compute_volume_attach_v2" "monitoring_storage_attach" {
  instance_id = ovh_cloud_project_instance.monitoring.id
  volume_id   = ovh_cloud_project_volume.monitoring_storage.id
}

# Grafana VM — no data volume; queries InfluxDB over the public :8086 endpoint.
resource "ovh_cloud_project_instance" "grafana" {
  service_name   = var.service_name
  region         = var.region
  billing_period = "hourly"
  name           = "${var.name}-ant-grafana-${var.region}"
  user_data      = local.enable_root_ssh_user_data

  boot_from {
    image_id = local.ubuntu_image_id
  }

  flavor {
    flavor_id = local.grafana_flavor_id
  }

  ssh_key {
    name = var.ssh_key_name
  }

  network {
    public = true
  }
}
