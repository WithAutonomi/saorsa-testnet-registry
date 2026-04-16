output "vm_ip" {
  value = digitalocean_droplet.client_vm.ipv4_address
}

output "vm_id" {
  value = digitalocean_droplet.client_vm.id
}
