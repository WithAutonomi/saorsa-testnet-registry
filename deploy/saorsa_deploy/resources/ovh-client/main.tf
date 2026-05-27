# Look up the flavor (e.g. b3-8) by name in the target region; the OVH
# instance resource needs a flavor_id, not a name.
data "ovh_cloud_project_flavors" "selected" {
  service_name = var.service_name
  region       = var.region
  name_filter  = var.flavor_name
}

# Look up Linux images in the region; pick the latest matching the chosen
# Ubuntu name pattern.
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

  # OVH's Ubuntu image disables root SSH and uses the 'ubuntu' cloud-init
  # user. The other providers (DO, Hetzner, Vultr) all allow root SSH on
  # their cloud images, and the client provisioner connects as root
  # uniformly, so enable root SSH on first boot to match.
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

resource "ovh_cloud_project_instance" "client_vm" {
  service_name   = var.service_name
  region         = var.region
  billing_period = "hourly"
  name           = "${var.name}-ant-client-${var.role}-${var.region}-${var.client_index}"
  user_data      = local.enable_root_ssh_user_data

  boot_from {
    image_id = local.ubuntu_image_id
  }

  flavor {
    flavor_id = local.flavor_id
  }

  ssh_key {
    name = var.ssh_key_name
  }

  network {
    public = true
  }
}
