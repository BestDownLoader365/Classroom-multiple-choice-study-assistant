const answerForm = document.querySelector("[data-answer-form]");

if (answerForm instanceof HTMLFormElement) {
    const inputs = [...answerForm.querySelectorAll("input[name='answers']")];
    const button = answerForm.querySelector("[data-submit-answer]");
    const hint = answerForm.querySelector("[data-selection-hint]");

    const updateSelection = () => {
        const selectedCount = inputs.filter((input) => input.checked).length;
        if (button instanceof HTMLButtonElement) {
            button.disabled = selectedCount === 0;
        }
        if (hint instanceof HTMLElement) {
            hint.textContent = selectedCount
                ? `已选择 ${selectedCount} 项`
                : "请至少选择一个答案";
            hint.classList.toggle("has-selection", selectedCount > 0);
        }
    };

    inputs.forEach((input) => input.addEventListener("change", updateSelection));
    updateSelection();

    answerForm.addEventListener("submit", (event) => {
        if (!inputs.some((input) => input.checked)) {
            event.preventDefault();
            updateSelection();
            return;
        }
        if (button instanceof HTMLButtonElement) {
            button.disabled = true;
            button.textContent = "正在提交…";
        }
    });
}

document.addEventListener("submit", (event) => {
    const form = event.target;
    if (
        !(form instanceof HTMLFormElement)
        || !form.matches("[data-confirm-restart], [data-confirm-reset], [data-confirm]")
    ) {
        return;
    }
    const message = form.dataset.confirmRestart
        || form.dataset.confirmReset
        || form.dataset.confirm
        || "确定要继续吗？";
    if (!window.confirm(message)) {
        event.preventDefault();
    }
});

const translations = document.querySelectorAll(".translation-zh");
const bilingualToggle = document.querySelector("[data-bilingual-toggle]");
if (translations.length > 0 && bilingualToggle instanceof HTMLButtonElement) {
    bilingualToggle.hidden = false;
    let bilingual = localStorage.getItem("mcq-bilingual") === "true";

    const renderLanguage = () => {
        document.body.classList.toggle("show-bilingual", bilingual);
        bilingualToggle.textContent = bilingual ? "仅显示英文" : "显示中英双语";
        bilingualToggle.setAttribute("aria-pressed", String(bilingual));
    };

    bilingualToggle.addEventListener("click", () => {
        bilingual = !bilingual;
        localStorage.setItem("mcq-bilingual", String(bilingual));
        renderLanguage();
    });
    renderLanguage();
}

const feedback = document.querySelector("[data-feedback]");
if (feedback instanceof HTMLElement) {
    feedback.focus({ preventScroll: true });
}

const initializePicker = (picker) => {
    const valueInput = picker.querySelector("[data-picker-value]");
    const trigger = picker.querySelector(".size-picker-trigger");
    const current = picker.querySelector("[data-select-current]");
    const menu = picker.querySelector("[data-select-menu]");
    const optionButtons = [...picker.querySelectorAll("[data-picker-option]")];

    if (
        !(valueInput instanceof HTMLInputElement)
        || !(trigger instanceof HTMLButtonElement)
        || !(current instanceof HTMLElement)
        || !(menu instanceof HTMLElement)
        || optionButtons.length === 0
    ) {
        return;
    }

    const renderSelection = () => {
        const selected = optionButtons.find(
            (button) => button.dataset.value === valueInput.value,
        ) || optionButtons[0];
        current.textContent = selected.textContent || "";
        optionButtons.forEach((button) => {
            button.setAttribute("aria-selected", String(button === selected));
        });
    };

    const isMenuOpen = () => picker.classList.contains("is-open");

    const closeMenu = (returnFocus = false) => {
        picker.classList.remove("is-open");
        trigger.setAttribute("aria-expanded", "false");
        menu.setAttribute("aria-hidden", "true");
        menu.inert = true;
        if (returnFocus) {
            trigger.focus();
        }
    };

    const openMenu = (focusIndex = null) => {
        picker.classList.remove("opens-up");
        menu.inert = false;
        menu.setAttribute("aria-hidden", "false");
        picker.classList.add("is-open");
        trigger.setAttribute("aria-expanded", "true");
        const triggerBox = trigger.getBoundingClientRect();
        const spaceBelow = window.innerHeight - triggerBox.bottom - 8;
        const spaceAbove = triggerBox.top - 8;
        if (menu.scrollHeight > spaceBelow && spaceAbove > spaceBelow) {
            picker.classList.add("opens-up");
        }
        if (Number.isInteger(focusIndex)) {
            optionButtons[focusIndex]?.focus();
        }
    };

    const chooseOption = (index) => {
        const option = optionButtons[index];
        if (!option) {
            return;
        }
        valueInput.value = option.dataset.value || "";
        valueInput.dispatchEvent(new Event("change", { bubbles: true }));
        picker.dispatchEvent(new CustomEvent("pickerchange", {
            bubbles: true,
            detail: { value: valueInput.value },
        }));
        renderSelection();
        closeMenu(true);
    };

    trigger.addEventListener("click", () => {
        if (isMenuOpen()) {
            closeMenu();
        } else {
            openMenu();
        }
    });

    trigger.addEventListener("keydown", (event) => {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            const offset = event.key === "ArrowDown" ? 1 : -1;
            const selectedIndex = Math.max(
                0,
                optionButtons.findIndex(
                    (button) => button.dataset.value === valueInput.value,
                ),
            );
            const index = Math.max(
                0,
                Math.min(optionButtons.length - 1, selectedIndex + offset),
            );
            openMenu(index);
        } else if (event.key === "Escape" && isMenuOpen()) {
            event.preventDefault();
            closeMenu();
        }
    });

    optionButtons.forEach((button, index) => {
        button.addEventListener("click", () => chooseOption(index));
        button.addEventListener("keydown", (event) => {
            let nextIndex = null;
            if (event.key === "ArrowDown") {
                nextIndex = Math.min(optionButtons.length - 1, index + 1);
            } else if (event.key === "ArrowUp") {
                nextIndex = Math.max(0, index - 1);
            } else if (event.key === "Home") {
                nextIndex = 0;
            } else if (event.key === "End") {
                nextIndex = optionButtons.length - 1;
            } else if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                chooseOption(index);
                return;
            } else if (event.key === "Escape") {
                event.preventDefault();
                closeMenu(true);
                return;
            } else if (event.key === "Tab") {
                closeMenu();
                return;
            }

            if (nextIndex !== null) {
                event.preventDefault();
                optionButtons[nextIndex]?.focus();
            }
        });
    });

    document.addEventListener("pointerdown", (event) => {
        if (!picker.contains(event.target)) {
            closeMenu();
        }
    });

    menu.inert = true;
    menu.setAttribute("aria-hidden", "true");
    renderSelection();
};

document.querySelectorAll("[data-picker]").forEach(initializePicker);

document.addEventListener("pickerchange", (event) => {
    if (!(event.target instanceof Element)) {
        return;
    }
    const form = event.target.closest("form[data-picker-submit-on-change]");
    if (form instanceof HTMLFormElement) {
        form.requestSubmit();
    }
});

document.querySelectorAll("[data-option-row]").forEach((row) => {
    const input = row.querySelector("input[name='answers']");
    if (!(input instanceof HTMLInputElement)) {
        return;
    }
    row.addEventListener("click", (event) => {
        if (event.target.closest("[data-glossary-term], input, label")) {
            return;
        }
        if (input.type === "radio") {
            input.checked = true;
        } else {
            input.checked = !input.checked;
        }
        input.dispatchEvent(new Event("change", { bubbles: true }));
    });
});

const allChapters = document.querySelector("[data-all-chapters]");
const specificChapters = [...document.querySelectorAll("[data-specific-chapter]")];
const chapterGroups = [...document.querySelectorAll("[data-chapter-group]")];

if (allChapters instanceof HTMLInputElement && specificChapters.length > 0) {
    const updateGroupState = (group) => {
        const groupToggle = group.querySelector("[data-chapter-group-all]");
        const groupChapters = [...group.querySelectorAll("[data-specific-chapter]")];
        if (!(groupToggle instanceof HTMLInputElement) || groupChapters.length === 0) {
            return;
        }

        const selectedCount = groupChapters.filter((chapter) => chapter.checked).length;
        groupToggle.checked = selectedCount === groupChapters.length;
        groupToggle.indeterminate = selectedCount > 0 && selectedCount < groupChapters.length;
    };

    const updateSelectionState = () => {
        chapterGroups.forEach(updateGroupState);
        allChapters.checked = !specificChapters.some((chapter) => chapter.checked);
    };

    allChapters.addEventListener("change", () => {
        if (allChapters.checked) {
            specificChapters.forEach((input) => { input.checked = false; });
        }
        updateSelectionState();
    });

    chapterGroups.forEach((group) => {
        const groupToggle = group.querySelector("[data-chapter-group-all]");
        const groupChapters = [...group.querySelectorAll("[data-specific-chapter]")];
        if (!(groupToggle instanceof HTMLInputElement) || groupChapters.length === 0) {
            return;
        }

        groupToggle.addEventListener("change", () => {
            groupChapters.forEach((chapter) => {
                chapter.checked = groupToggle.checked;
            });
            updateSelectionState();
        });
    });

    specificChapters.forEach((input) => {
        input.addEventListener("change", updateSelectionState);
    });

    updateSelectionState();
}

const examAnswerForm = document.querySelector("[data-exam-answer-form]");

if (examAnswerForm instanceof HTMLFormElement) {
    // Mock exams allow saving an empty selection (it clears the slot), so the
    // submit buttons stay enabled; only the hint mirrors the selection count.
    const examInputs = [...examAnswerForm.querySelectorAll("input[name='answers']")];
    const examHint = examAnswerForm.querySelector("[data-exam-hint]");

    const updateExamHint = () => {
        const selectedCount = examInputs.filter((input) => input.checked).length;
        if (examHint instanceof HTMLElement) {
            examHint.textContent = selectedCount
                ? `已选择 ${selectedCount} 项，保存后可随时修改`
                : "未选择任何选项，保存将清空本题答案";
            examHint.classList.toggle("has-selection", selectedCount > 0);
        }
    };

    examInputs.forEach((input) => input.addEventListener("change", updateExamHint));
}

const examTimer = document.querySelector("[data-exam-timer]");

if (examTimer instanceof HTMLElement) {
    // The server remains the authority for the deadline; this countdown only
    // mirrors it and triggers the same submit form when it reaches zero.
    const submitForm = document.querySelector("[data-exam-submit-form]");
    let remaining = Number.parseInt(examTimer.dataset.remainingSeconds || "0", 10);

    const renderTimer = () => {
        const total = Math.max(0, remaining);
        const hours = Math.floor(total / 3600);
        const minutes = Math.floor((total % 3600) / 60);
        const seconds = total % 60;
        const padded = `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
        examTimer.textContent = hours > 0
            ? `剩余 ${hours}:${padded}`
            : `剩余 ${padded}`;
        examTimer.classList.toggle("is-low", total > 0 && total <= 60);
    };

    renderTimer();
    const tick = window.setInterval(() => {
        remaining -= 1;
        renderTimer();
        if (remaining <= 0) {
            window.clearInterval(tick);
            if (submitForm instanceof HTMLFormElement) {
                // Time ran out: submit directly without the confirm dialog.
                submitForm.removeAttribute("data-confirm");
                submitForm.requestSubmit();
            }
        }
    }, 1000);
}
