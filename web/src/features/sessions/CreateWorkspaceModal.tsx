import { Alert, Button, Modal, Stack, TextInput } from "@mantine/core";
import { useState } from "react";

import { useCreateWorkspaceMutation } from "../../api/helpermeApi";

type CreateWorkspaceModalProps = {
  opened: boolean;
  onClose: () => void;
};

/**
 * 工作区只能由用户显式建：一个名字 + 一个已存在的目录。
 * 目录不存在、或已被别的工作区占用时，后端会拒绝（这里只说清两种原因）。
 */
export function CreateWorkspaceModal({
  opened,
  onClose,
}: CreateWorkspaceModalProps) {
  const [name, setName] = useState("");
  const [taskRoot, setTaskRoot] = useState("");
  const [createWorkspace, { isLoading, isError, reset }] =
    useCreateWorkspaceMutation();
  const canSubmit = name.trim() !== "" && taskRoot.trim() !== "";

  function close() {
    setName("");
    setTaskRoot("");
    reset();
    onClose();
  }

  return (
    <Modal onClose={close} opened={opened} title="新建工作区">
      <Stack gap="sm">
        <TextInput
          label="名称"
          onChange={(event) => setName(event.currentTarget.value)}
          placeholder="ai_charter"
          value={name}
        />
        <TextInput
          description="本机上已存在的目录；工作区的文件访问不会越出它"
          label="目录"
          onChange={(event) => setTaskRoot(event.currentTarget.value)}
          placeholder="E:/work/ai_charter"
          value={taskRoot}
        />
        {isError ? (
          <Alert color="red" title="创建失败">
            目录不存在，或者已经被另一个工作区占用。
          </Alert>
        ) : null}
        <Button
          disabled={!canSubmit}
          loading={isLoading}
          onClick={() => {
            void createWorkspace({
              name: name.trim(),
              taskRoot: taskRoot.trim(),
              fullAccess: false,
            })
              .unwrap()
              .then(close, () => undefined);
          }}
        >
          创建
        </Button>
      </Stack>
    </Modal>
  );
}
