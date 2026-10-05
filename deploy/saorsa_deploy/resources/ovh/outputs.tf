output "vm_ips" {
  description = "Public IPv4 addresses of the created instances, ordered by count.index."
  value = [
    # OVH sometimes attaches a SECOND public IPv4 to an instance. `one()` raises
    # "Invalid function argument" on a 2-element list, which broke `apply` AND
    # `destroy` — destroy evaluates outputs too, so the region became
    # undestroyable and had to be cleared through the OVH API by hand
    # (V2-1424 / DEV-06 DE1, 2026-10-05). `addresses` is a set, so iteration
    # order is not guaranteed: sort first, then take the lowest address, so the
    # value is stable across runs. `try(..., null)` keeps `one()`'s old
    # empty-list result and guarantees this output can never fail a destroy.
    for inst in ovh_cloud_project_instance.node_vm :
    try(sort([for a in inst.addresses : a.ip if a.version == 4])[0], null)
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
