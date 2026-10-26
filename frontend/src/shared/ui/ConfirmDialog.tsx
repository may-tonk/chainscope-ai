/**
 * 确认对话框：危险或不可撤销的操作（注销账户，以及以后的删除报告、删除地址标记等）执行前再确认一次。
 *
 * 用浏览器原生的 <dialog> 元素的“模态”模式（showModal）实现，浏览器自动处理：
 * - 对话框打开时，后面的页面不能点击，键盘焦点也不会跑到后面去；
 * - 按 Esc 关闭（等同于点“取消”）；
 * - 读屏软件会读出这是一个对话框，以及它的标题。
 * 这样不需要自己写一套遮罩和焦点管理，代码少、行为也和系统一致。
 *
 * 用法：
 *   <ConfirmDialog open={open} title="确认注销账户？" confirmLabel="确认注销" variant="danger"
 *     loading={submitting} onConfirm={...} onCancel={() => setOpen(false)}>
 *     说明文字……
 *   </ConfirmDialog>
 */
import { useEffect, useId, useRef, type ReactNode } from "react";

import { Button } from "./Button";
import styles from "./ConfirmDialog.module.css";

interface ConfirmDialogProps {
  /** 是否打开。由调用方控制。 */
  open: boolean;
  /** 对话框标题，例如“确认注销账户？”。 */
  title: string;
  /** 说明文字：要做什么、后果是什么。 */
  children: ReactNode;
  /** 确认按钮的文字，例如“确认注销”。写清楚动作，不用笼统的“确定”。 */
  confirmLabel: string;
  /** 取消按钮的文字，默认“取消”。 */
  cancelLabel?: string;
  /** danger：确认按钮为红色（危险操作）；primary：普通确认。 */
  variant?: "danger" | "primary";
  /** 正在执行：确认按钮转圈，两个按钮都不能点，Esc 也不能关闭。 */
  loading?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/** 确认对话框组件：标题、说明、取消和确认两个按钮。 */
export function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel,
  cancelLabel = "取消",
  variant = "primary",
  loading = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const titleId = useId();

  // 把 React 的 open 状态同步到原生 <dialog>：打开时调用 showModal()，关闭时调用 close()。
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) {
      return;
    }
    if (open && !dialog.open) {
      dialog.showModal();
    } else if (!open && dialog.open) {
      dialog.close();
    }
  }, [open]);

  return (
    <dialog
      ref={dialogRef}
      className={styles.dialog}
      aria-labelledby={titleId}
      // 按 Esc 时浏览器会触发 cancel 事件：执行中不允许关闭；否则交给调用方处理（等同点“取消”）。
      onCancel={(event) => {
        event.preventDefault();
        if (!loading) {
          onCancel();
        }
      }}
    >
      <h2 id={titleId} className={styles.title}>
        {title}
      </h2>
      <div className={styles.body}>{children}</div>
      <div className={styles.actions}>
        {/* 默认焦点放在“取消”上：误按回车不会直接执行危险操作。 */}
        <Button variant="secondary" onClick={onCancel} disabled={loading} autoFocus>
          {cancelLabel}
        </Button>
        <Button variant={variant} onClick={onConfirm} loading={loading}>
          {confirmLabel}
        </Button>
      </div>
    </dialog>
  );
}
