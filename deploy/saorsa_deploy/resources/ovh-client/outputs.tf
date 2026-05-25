output "vm_ip" {
  description = "Public IPv4 address of the created instance."
  value = one([
    for a in ovh_cloud_project_instance.client_vm.addresses : a.ip
    if a.version == 4
  ])
}

output "vm_id" {
  description = "OVH/OpenStack instance ID."
  value       = ovh_cloud_project_instance.client_vm.id
}
