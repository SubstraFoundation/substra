# Copyright 2018 Owkin, inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from copy import deepcopy
import logging
import json

from substra.sdk import exceptions, schemas, compute_plan
from substra.sdk.backends import base
from substra.sdk.backends.remote import rest_client

logger = logging.getLogger(__name__)

DEFAULT_RETRY_TIMEOUT = 5 * 60
AUTO_BATCHING = "auto_batching"
BATCH_SIZE = 'batch_size'


def _get_asset_key(data):
    return data.get('pkhash') or data.get('key') or data.get('computePlanID')


def _find_asset_field(data, field):
    """Find data value where location is defined as `field.subfield...`."""
    for f in field.split('.'):
        data = data[f]
    return data


class Remote(base.BaseBackend):

    def __init__(self, url, insecure, token, retry_timeout):
        self._client = rest_client.Client(url, insecure, token)
        self._retry_timeout = retry_timeout or DEFAULT_RETRY_TIMEOUT

    def login(self, username, password):
        return self._client.login(username, password)

    def get(self, asset_type, key):
        """Get an asset by key."""
        return self._client.get(asset_type.to_server(), key)

    def list(self, asset_type, filters=None):
        """List assets per asset type."""
        return self._client.list(asset_type.to_server(), filters)

    def _add(self, asset, data, files=None, exist_ok=False):
        data = deepcopy(data)  # make a deep copy for avoiding modification by reference
        if files:
            kwargs = {
                'data': {
                    'json': json.dumps(data),
                },
                'files': files,
            }

        else:
            kwargs = {
                'json': data,
            }

        return self._client.add(
            asset.to_server(),
            retry_timeout=self._retry_timeout,
            exist_ok=exist_ok,
            **kwargs,
        )

    def _add_data_samples(self, spec, exist_ok, spec_options):
        """Add data sample(s)."""
        # data sample(s) creation must be handled separately as the get request is not
        # available on this ressource. On top of that the exist ok option is working
        # only when adding a single data sample (and not for bulk upload)
        try:
            with spec.build_request_kwargs(**spec_options) as (data, files):
                data_samples = self._add(
                    schemas.Type.DataSample, data, files, exist_ok=exist_ok)

        except exceptions.AlreadyExists as e:
            if not exist_ok or spec.is_many():
                raise

            key = e.pkhash[0]
            logger.warning(f"data_sample already exists: key='{key}'")
            data_samples = [{'pkhash': key}]

        # there is currently a single route in the backend to add a single or many
        # datasamples, this route always returned a list of created data sample keys
        return data_samples if spec.is_many() else data_samples[0]

    def add(self, spec, exist_ok=False, spec_options=None):
        """Add an asset."""
        spec_options = spec_options or {}
        asset_type = spec.__class__.type_
        # Remove batch_size from spec_options
        batch_size = spec_options.pop(BATCH_SIZE, None)

        if asset_type == schemas.Type.DataSample:
            # data sample corner case
            return self._add_data_samples(
                spec,
                exist_ok=exist_ok,
                spec_options=spec_options,
            )
        elif asset_type == schemas.Type.ComputePlan and spec_options.pop(AUTO_BATCHING, False):
            if not batch_size:
                raise ValueError("Batch size must be defined to create a compute plan \
                    with the auto-batching feature.")
            # Compute plan auto batching feature
            return self._auto_batching_compute_plan(
                spec=spec,
                exist_ok=exist_ok,
                batch_size=batch_size,
                spec_options=spec_options,
            )

        with spec.build_request_kwargs(**spec_options) as (data, files):
            response = self._add(asset_type, data, files=files, exist_ok=exist_ok)
        # The backend has inconsistent API responses when getting or adding an asset
        # (with much less data when responding to adds).
        # A second GET request hides the discrepancies.
        # Do not do this with a compute plan or we lose the id_to_key field
        if asset_type == schemas.Type.ComputePlan:
            return response

        key = _get_asset_key(response)
        return self.get(asset_type, key)

    def _auto_batching_compute_plan(self,
                                    spec,
                                    batch_size,
                                    compute_plan_id=None,
                                    exist_ok=None,
                                    spec_options=None):
        """Auto batching of the compute plan tuples

        It computes the batches then, for each batch, it calls the 'add' and
        'update_compute_plan' methods with auto_batching=False.
        """
        spec_options = spec_options or dict()
        spec_options[AUTO_BATCHING] = False
        spec_options[BATCH_SIZE] = batch_size

        batches = compute_plan.auto_batching(
            spec,
            is_creation=compute_plan_id is None,
            batch_size=batch_size,
        )

        id_to_keys = dict()
        asset = None

        # Create the compute plan if it does not exist
        if not compute_plan_id:
            first_spec = next(batches, None)
            tmp_spec = first_spec or spec  # Special case: no tuples
            asset = self.add(spec=tmp_spec, exist_ok=exist_ok, spec_options=deepcopy(spec_options))
            compute_plan_id = asset['computePlanID']
            id_to_keys = asset['IDToKey']

        # Update the compute plan
        for tmp_spec in batches:
            asset = self.update_compute_plan(
                compute_plan_id=compute_plan_id,
                spec=tmp_spec,
                spec_options=deepcopy(spec_options)
            )
            id_to_keys.update(asset['IDToKey'])

        # Special case: no tuples
        if asset is None:
            return self.get(
                asset_type=schemas.Type.ComputePlan,
                key=compute_plan_id,
            )

        asset['IDToKey'] = id_to_keys
        return asset

    def update_compute_plan(self, compute_plan_id, spec, spec_options=None):
        spec_options = spec_options or {}
        batch_size = spec_options.pop(BATCH_SIZE)
        if spec_options.pop(AUTO_BATCHING):
            return self._auto_batching_compute_plan(
                spec=spec,
                compute_plan_id=compute_plan_id,
                batch_size=batch_size,
                spec_options=spec_options,
            )
        else:
            # Disable auto batching
            return self._client.request(
                'post',
                schemas.Type.ComputePlan.to_server(),
                path=f"{compute_plan_id}/update_ledger/",
                json=spec.dict(exclude_none=True),
            )

    def link_dataset_with_objective(self, dataset_key, objective_key):
        return self._client.request(
            'post',
            schemas.Type.Dataset.to_server(),
            path=f"{dataset_key}/update_ledger/",
            data={'objective_key': objective_key, },
        )

    def link_dataset_with_data_samples(self, dataset_key, data_sample_keys):
        data = {
            'data_manager_keys': [dataset_key],
            'data_sample_keys': data_sample_keys,
        }
        return self._client.request(
            'post',
            schemas.Type.DataSample.to_server(),
            path="bulk_update/",
            data=data,
        )

    def download(self, asset_type, url_field_path, key, destination):
        data = self.get(asset_type, key)
        url = _find_asset_field(data, url_field_path)
        response = self._client.get_data(url, stream=True)
        chunk_size = 1024
        with open(destination, 'wb') as f:
            for chunk in response.iter_content(chunk_size):
                f.write(chunk)

        return destination

    def describe(self, asset_type, key):
        data = self.get(asset_type, key)
        url = data['description']['storageAddress']
        r = self._client.get_data(url)
        return r.text

    def leaderboard(self, objective_key, sort='desc'):
        return self._client.request(
            'get',
            schemas.Type.Objective.to_server(),
            f'{objective_key}/leaderboard',
            params={'sort': sort},
        )

    def cancel_compute_plan(self, compute_plan_id):
        return self._client.request(
            'post',
            schemas.Type.ComputePlan.to_server(),
            path=f"{compute_plan_id}/cancel",
        )