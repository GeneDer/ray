// Copyright 2020 The Ray Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//  http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#include "ray/rpc/worker/core_worker_client_pool.h"

#include <memory>
#include <string>
#include <utility>

namespace ray {
namespace rpc {

std::function<void()> CoreWorkerClientPool::GetDefaultUnavailableTimeoutCallback(
    gcs::GcsClient *gcs_client,
    rpc::CoreWorkerClientPool *worker_client_pool,
    std::function<std::shared_ptr<RayletClientInterface>(std::string, int32_t)>
        raylet_client_factory,
    const rpc::Address &addr) {
  return [addr,
          gcs_client,
          worker_client_pool,
          raylet_client_factory = std::move(raylet_client_factory)]() {
    const NodeID node_id = NodeID::FromBinary(addr.raylet_id());
    const WorkerID worker_id = WorkerID::FromBinary(addr.worker_id());
    RAY_CHECK(gcs_client->Nodes().IsSubscribedToNodeChange());
    const rpc::GcsNodeInfo *node_info =
        gcs_client->Nodes().Get(node_id, /*filter_dead_nodes=*/true);
    if (node_info == nullptr) {
      RAY_LOG(INFO).WithField(worker_id).WithField(node_id)
          << "Disconnect core worker client since its node is dead";
      worker_client_pool->Disconnect(worker_id);
      return;
    }
    auto raylet_client = raylet_client_factory(node_info->node_manager_address(),
                                               node_info->node_manager_port());
    raylet_client->IsLocalWorkerDead(
        worker_id,
        [worker_client_pool, worker_id, node_id](const Status &status,
                                                 rpc::IsLocalWorkerDeadReply &&reply) {
          if (!status.ok()) {
            RAY_LOG(INFO).WithField(worker_id).WithField(node_id)
                << "Failed to check if worker is dead on request to raylet";
            return;
          }
          if (reply.is_dead()) {
            RAY_LOG(INFO).WithField(worker_id)
                << "Disconnect core worker client since it is dead";
            worker_client_pool->Disconnect(worker_id);
          }
        });
  };
}

std::shared_ptr<CoreWorkerClientInterface> CoreWorkerClientPool::GetOrConnect(
    const Address &addr_proto) {
  RAY_CHECK_NE(addr_proto.worker_id(), "");
  absl::MutexLock lock(&mu_);

  RemoveIdleClients();

  CoreWorkerClientEntry entry;
  auto id = WorkerID::FromBinary(addr_proto.worker_id());
  auto it = client_map_.find(id);
  if (it != client_map_.end()) {
    entry = *it->second;
    client_list_.erase(it->second);
  } else {
    entry = CoreWorkerClientEntry(id, core_worker_client_factory_(addr_proto));
  }
  client_list_.emplace_front(entry);
  client_map_[id] = client_list_.begin();

  RAY_LOG(DEBUG) << "Connected to worker " << id << " with address "
                 << addr_proto.ip_address() << ":" << addr_proto.port();
  return entry.core_worker_client;
}

void CoreWorkerClientPool::RemoveIdleClients() {
  while (!client_list_.empty()) {
    auto id = client_list_.back().worker_id;
    // The last client in the list is the least recent accessed client.
    if (client_list_.back().core_worker_client->IsIdleAfterRPCs()) {
      client_map_.erase(id);
      client_list_.pop_back();
      RAY_LOG(DEBUG) << "Remove idle client to worker " << id
                     << " , num of clients is now " << client_list_.size();
    } else {
      auto entry = client_list_.back();
      client_list_.pop_back();
      client_list_.emplace_front(entry);
      client_map_[id] = client_list_.begin();
      break;
    }
  }
}

void CoreWorkerClientPool::Disconnect(ray::WorkerID id) {
  absl::MutexLock lock(&mu_);
  auto it = client_map_.find(id);
  if (it == client_map_.end()) {
    return;
  }
  client_list_.erase(it->second);
  client_map_.erase(it);
}

}  // namespace rpc
}  // namespace ray
