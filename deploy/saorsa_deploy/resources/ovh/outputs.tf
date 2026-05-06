output "vm_ips" {
  description = "Public IPv4 addresses of the created instances, ordered by count.index."
  value = [
    for inst in ovh_cloud_project_instance.node_vm :
    one([for a in inst.addresses : a.ip if a.version == 4])
  ]
}

output "vm_ids" {
  description = "OVH/OpenStack instance IDs."
  value       = [for inst in ovh_cloud_project_instance.node_vm : inst.id]
}

output "volume_ids" {
  description = "OVH block storage volume IDs."
  value       = [for v in ovh_cloud_project_volume.node_storage : v.id]
}
