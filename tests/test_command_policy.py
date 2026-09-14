import unittest

from bridge.command_policy import HARD_DELETE, OPAQUE, REVIEW, classify


class CommandPolicyTests(unittest.TestCase):
    def assert_effect(self, command, effect):
        self.assertEqual(classify(command).effect, effect, command)

    def test_quoted_delete_words_are_not_commands(self):
        for command in [
            "echo 'rm -rf /'",
            "printf '%s' 'DROP TABLE users'",
            "grep -R \"docker volume rm\" docs/",
        ]:
            with self.subTest(command=command):
                self.assert_effect(command, REVIEW)

    def test_direct_hard_delete_commands_require_human(self):
        for command in [
            "rm file.txt",
            "unlink file.txt",
            "rmdir old",
            "shred data.bin",
            "truncate -s 0 app.db",
            "mkfs.ext4 /dev/disk9",
            "find . -name '*.tmp' -delete",
            "git clean -fd",
            "git reset --hard HEAD",
            "git branch -D feature",
            "docker rm app",
            "docker volume prune -f",
            "podman image rm old:latest",
            "sqlite3 app.db 'DELETE FROM users'",
            "psql -c 'DROP TABLE users'",
            "mysql -e 'TRUNCATE TABLE sessions'",
            "kubectl delete pod app",
            "aws s3 rm s3://bucket/key",
            "gcloud compute instances delete vm",
            "terraform destroy -auto-approve",
            "curl -X DELETE https://api.example.test/item/1",
            "curl --request DELETE https://api.example.test/item/1",
        ]:
            with self.subTest(command=command):
                self.assert_effect(command, HARD_DELETE)

    def test_wrapped_hard_delete_still_requires_human(self):
        for command in [
            "sudo rm file.txt",
            "sudo -u deploy rm file.txt",
            "sudo --user deploy rm file.txt",
            "env FOO=1 rm file.txt",
            "env -i PATH=/bin rm file.txt",
            "bash -c 'rm file.txt'",
            "python -c 'import os; os.unlink(\"data\")'",
            "node -e 'require(\"fs\").rmSync(\"data\", {recursive:true})'",
            "eval 'rm file.txt'",
        ]:
            with self.subTest(command=command):
                self.assert_effect(command, HARD_DELETE)

    def test_opaque_payloads_are_not_delegated_to_pm(self):
        for command in [
            "bash -c 'pytest tests'",
            "python scripts/build.py",
            "python -c 'print(1)'",
            "node -e 'console.log(1)'",
            "source ./env.sh",
            "echo $(rm file.txt)",
            "echo `rm file.txt`",
            "cat <<EOF\nhello\nEOF",
            "echo cm0gZmlsZQ== | base64 -d | sh",
        ]:
            with self.subTest(command=command):
                self.assert_effect(command, OPAQUE)

    def test_later_hard_delete_beats_earlier_opaque_segment(self):
        self.assert_effect("python scripts/build.py; rm file.txt", HARD_DELETE)
        self.assert_effect("python scripts/build.py\nrm file.txt", HARD_DELETE)
        self.assert_effect("echo 'hello\nrm file.txt'", REVIEW)

    def test_routine_development_commands_remain_reviewable(self):
        for command in [
            "docker restart app",
            "git status --short",
            "git commit -m fix",
            "pytest tests/test_pm_policy.py",
            "appctl build",
            "hermes-approval-bridge soft-delete /workspace cache",
        ]:
            with self.subTest(command=command):
                self.assert_effect(command, REVIEW)


if __name__ == "__main__":
    unittest.main()
