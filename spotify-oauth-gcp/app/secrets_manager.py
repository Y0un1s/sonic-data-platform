from google.cloud import secretmanager
import json
import base64

class SecretManagerClient:
    def __init__(self, project_id):
        self.project_id = project_id
        self.client = secretmanager.SecretManagerServiceClient()

    def create_or_update_secret(self, secret_id: str, payload: str):
        parent = f"projects/{self.project_id}"

        try:
            self.client.get_secret(request={"name": f"{parent}/secrets/{secret_id}"})
        except Exception:
            self.client.create_secret(
                request={
                    "parent": parent,
                    "secret_id": secret_id,
                    "secret": {"replication": {"automatic": {}}},
                }
            )

        self.client.add_secret_version(
            request={
                "parent": f"{parent}/secrets/{secret_id}",
                "payload": {"data": payload.encode("utf-8")},
            }
        )

    def get_secret_payload(self, secret_id: str):
        name = f"projects/{self.project_id}/secrets/{secret_id}/versions/latest"
        try:
            response = self.client.access_secret_version(request={"name": name})
            payload = response.payload.data.decode("utf-8")
            return json.loads(payload)
        except Exception:
            return None

    def list_spotify_secrets(self, prefix: str):
        parent = f"projects/{self.project_id}"
        all_secrets = [s.name.split("/")[-1] for s in self.client.list_secrets(request={"parent": parent})]
        return [n for n in all_secrets if n.startswith(prefix)]
