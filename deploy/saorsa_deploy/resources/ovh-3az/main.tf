# Look up the flavor by name in the target region (Nova needs a flavor_id).
data "ovh_cloud_project_flavors" "selected" {
  service_name = var.service_name
  region       = var.region
  name_filter  = var.flavor_name
}

# Look up Linux images in the region; pick the latest matching the Ubuntu name.
data "ovh_cloud_project_images" "linux" {
  service_name = var.service_name
  region       = var.region
  os_type      = "linux"
}

locals {
  flavor_id = one(data.ovh_cloud_project_flavors.selected.flavors).id

  ubuntu_images = [
    for img in data.ovh_cloud_project_images.linux.images :
    img if can(regex(var.ubuntu_image_name_pattern, img.name))
  ]
  ubuntu_image_id = local.ubuntu_images[0].id

  # Same cloud-init as resources/ovh: OVH images use the 'ubuntu' user, so enable
  # root SSH on first boot to keep the downstream provisioner connecting as root
  # uniformly across providers. Nova accepts user_data (the OVH-native API 500s).
  user_data = <<-EOT
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

# OpenStack-compatible user for the OpenStack/Nova + Cinder operations.
resource "ovh_cloud_project_user" "attach_user" {
  service_name = var.service_name
  description  = "saorsa-deploy: openstack auth for ${var.name}-${var.region} (3az)"
  role_names   = ["compute_operator", "volume_operator"]
}

# Per-deployment Nova keypair from the operator's public key.
resource "openstack_compute_keypair_v2" "deploy_key" {
  name       = "${lower(var.name)}-${lower(var.region)}-node"
  public_key = file(pathexpand(var.ssh_public_key_path))
}

resource "openstack_compute_instance_v2" "node_vm" {
  count     = var.vm_count
  name      = "${var.name}-${var.name_template}-${var.region}-${count.index + 1 + var.name_index_offset}"
  flavor_id = local.flavor_id
  image_id  = local.ubuntu_image_id
  key_pair  = openstack_compute_keypair_v2.deploy_key.name
  user_data = local.user_data

  # Round-robin across the region's AZs so VMs spread over all three zones; empty
  # list -> null -> Nova auto-places. (Instance AZ works via Nova; the OVH-native
  # API 500s on it.)
  availability_zone = length(var.availability_zones) > 0 ? var.availability_zones[count.index % length(var.availability_zones)] : null

  network {
    name = var.public_network_name
  }
}

# NO attached data volume: OVH 3-AZ regions (EU-WEST-PAR, EU-SOUTH-MIL) do not
# offer Cinder block storage at all (verified 2026-07-10 — the region exposes no
# `volume` service; a create goes to `error` status). These nodes store data on
# the instance root disk (b3-16 ≈ 94 GB usable), so the provisioner must use a
# root-disk /mnt/ant-node-storage directory rather than mounting a block device.
