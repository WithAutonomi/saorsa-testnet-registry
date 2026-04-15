output "vm_ip" {
  value = digitalocean_droplet.bootstrap_vm.ipv4_address
}

output "vm_id" {
  value = digitalocean_droplet.bootstrap_vm.id
}

output "volume_id" {
  value = digitalocean_volume.bootstrap_storage.id
}
