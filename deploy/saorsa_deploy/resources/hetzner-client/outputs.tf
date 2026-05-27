output "vm_ip" {
  value = hcloud_server.client_vm.ipv4_address
}

output "vm_id" {
  value = hcloud_server.client_vm.id
}
