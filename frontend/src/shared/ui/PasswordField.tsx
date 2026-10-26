/**
 * 密码输入框：在普通输入框右侧加一个“眼睛”按钮，可以切换显示/隐藏密码。
 *
 * 方案第 5 节要求支持密码管理器和粘贴，所以这里不禁止粘贴、不禁止自动填充。
 * 调用方通过 autoComplete 告诉浏览器和密码管理器这是哪种密码：
 * - "current-password"：登录时输入已有密码（密码管理器会自动填入）；
 * - "new-password"：注册、改密码时设置新密码（密码管理器会提议生成强密码）。
 */
import { Eye, EyeOff } from "lucide-react";
import { useState } from "react";

import { TextField, type TextFieldProps } from "./TextField";
import styles from "./PasswordField.module.css";

type PasswordFieldProps = Omit<TextFieldProps, "type" | "trailing"> & {
  autoComplete: "current-password" | "new-password";
};

/** 密码框组件：其余参数原样交给 TextField，只额外控制“显示/隐藏”和输入类型。 */
export function PasswordField(props: PasswordFieldProps) {
  const [visible, setVisible] = useState(false);
  const label = visible ? "隐藏密码" : "显示密码";
  return (
    <TextField
      {...props}
      type={visible ? "text" : "password"}
      // 关闭拼写检查和自动大写：否则浏览器可能把密码发给拼写检查服务，或在手机上自动改成首字母大写。
      spellCheck={false}
      autoCapitalize="off"
      autoCorrect="off"
      trailing={
        <button
          type="button"
          className={styles.toggle}
          onClick={() => setVisible((v) => !v)}
          aria-label={label}
          aria-pressed={visible}
          title={label}
        >
          {/* 图标表示“当前状态”（2026-10-26 用户指出，按通用习惯）：
              密码隐藏（默认）→ 闭眼；点击后密码可见 → 睁眼。
              按钮的 aria-label 仍写“点了会发生什么”（显示密码 / 隐藏密码），读屏软件用户才知道它的作用。 */}
          {visible ? <Eye size={18} aria-hidden="true" /> : <EyeOff size={18} aria-hidden="true" />}
        </button>
      }
    />
  );
}
