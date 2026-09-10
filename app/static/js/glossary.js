(() => {
    const dataElement = document.getElementById("glossary-data");
    if (!(dataElement instanceof HTMLScriptElement)) {
        return;
    }

    let glossary;
    try {
        glossary = JSON.parse(dataElement.textContent || "{}");
    } catch (_error) {
        return;
    }
    if (!Array.isArray(glossary.terms)) {
        return;
    }

    const termsById = new Map(glossary.terms.map((term) => [term.id, term]));
    const escapePattern = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const candidates = glossary.terms.flatMap((term, termOrder) =>
        [term.term, ...(term.aliases || [])].map((label, labelOrder) => ({
            label,
            term,
            termOrder,
            labelOrder,
        })),
    ).sort((left, right) => (
        Array.from(right.label).length - Array.from(left.label).length
        || left.termOrder - right.termOrder
        || left.labelOrder - right.labelOrder
    ));
    const matcher = candidates.length
        ? new RegExp(candidates.map(({ label }) => escapePattern(label)).join("|"), "giu")
        : null;
    const candidateByLabel = new Map(
        candidates.map((candidate) => [candidate.label.toLocaleLowerCase(), candidate]),
    );
    const wordCharacter = /[\p{L}\p{N}\p{M}_]/u;

    const hasSafeBoundary = (text, start, matched) => {
        const first = Array.from(matched)[0] || "";
        const last = Array.from(matched).at(-1) || "";
        const before = Array.from(text.slice(0, start)).at(-1) || "";
        const after = Array.from(text.slice(start + matched.length))[0] || "";
        return !(
            (wordCharacter.test(first) && wordCharacter.test(before))
            || (wordCharacter.test(last) && wordCharacter.test(after))
        );
    };

    let activeTerm = null;
    let activeTrigger = null;
    const popover = document.createElement("aside");
    popover.className = "glossary-popover";
    popover.id = "glossary-popover";
    popover.setAttribute("role", "dialog");
    popover.setAttribute("aria-label", "专业词汇释义");
    popover.hidden = true;

    const popoverAlias = document.createElement("p");
    popoverAlias.className = "glossary-popover-alias";
    const popoverTerm = document.createElement("h2");
    const popoverChinese = document.createElement("p");
    popoverChinese.className = "glossary-popover-zh";
    const popoverDefinitionZh = document.createElement("p");
    popoverDefinitionZh.className = "glossary-popover-definition";
    const popoverDefinitionEn = document.createElement("p");
    popoverDefinitionEn.className = "glossary-popover-definition-en";
    const popoverClose = document.createElement("button");
    popoverClose.className = "glossary-popover-close";
    popoverClose.type = "button";
    popoverClose.setAttribute("aria-label", "关闭专业词汇释义");
    popoverClose.textContent = "×";
    popover.append(
        popoverClose,
        popoverAlias,
        popoverTerm,
        popoverChinese,
        popoverDefinitionZh,
        popoverDefinitionEn,
    );
    document.body.append(popover);

    const positionPopover = () => {
        if (popover.hidden || !(activeTrigger instanceof HTMLElement)) {
            return;
        }
        const anchor = activeTrigger.getBoundingClientRect();
        const panel = popover.getBoundingClientRect();
        const gutter = 12;
        const viewportWidth = document.documentElement.clientWidth;
        const viewportHeight = document.documentElement.clientHeight;
        let left = anchor.left + anchor.width / 2 - panel.width / 2;
        left = Math.max(gutter, Math.min(left, viewportWidth - panel.width - gutter));
        let top = anchor.bottom + 9;
        if (top + panel.height > viewportHeight - gutter && anchor.top > panel.height + gutter) {
            top = anchor.top - panel.height - 9;
        }
        popover.style.left = `${Math.round(left)}px`;
        popover.style.top = `${Math.max(gutter, Math.round(top))}px`;
    };

    const closePopover = (returnFocus = false) => {
        if (activeTrigger instanceof HTMLElement) {
            activeTrigger.setAttribute("aria-expanded", "false");
            if (returnFocus) {
                activeTrigger.focus();
            }
        }
        popover.hidden = true;
        popover.classList.remove("is-open");
        activeTerm = null;
        activeTrigger = null;
    };

    const openPopover = (trigger) => {
        const term = termsById.get(trigger.dataset.glossaryTerm);
        if (!term) {
            return;
        }
        if (activeTrigger && activeTrigger !== trigger) {
            activeTrigger.setAttribute("aria-expanded", "false");
        }
        activeTerm = term;
        activeTrigger = trigger;
        const displayed = trigger.textContent || term.term;
        const isAlias = displayed.toLocaleLowerCase() !== term.term.toLocaleLowerCase();
        popoverAlias.textContent = isAlias ? displayed : "专业词汇";
        popoverTerm.textContent = term.term;
        popoverChinese.textContent = term.term_zh;
        popoverDefinitionZh.textContent = term.definition_zh || "";
        popoverDefinitionZh.hidden = !term.definition_zh;
        popoverDefinitionEn.textContent = term.definition || "";
        popoverDefinitionEn.hidden = !term.definition;
        trigger.setAttribute("aria-expanded", "true");
        popover.hidden = false;
        requestAnimationFrame(() => {
            popover.classList.add("is-open");
            positionPopover();
        });
    };

    const createTermButton = (matched, term) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "glossary-term";
        button.dataset.glossaryTerm = term.id;
        button.setAttribute("aria-controls", popover.id);
        button.setAttribute("aria-expanded", "false");
        button.setAttribute("aria-label", `${matched}，查看中文释义`);
        button.textContent = matched;
        button.addEventListener("click", (event) => {
            event.preventDefault();
            event.stopPropagation();
            openPopover(button);
        });
        button.addEventListener("keydown", (event) => {
            if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                event.stopPropagation();
                openPopover(button);
            } else if (event.key === "Escape") {
                event.preventDefault();
                closePopover(true);
            }
        });
        return button;
    };

    const highlightTextNode = (node) => {
        const text = node.nodeValue || "";
        matcher.lastIndex = 0;
        const matches = [];
        let match;
        while ((match = matcher.exec(text)) !== null) {
            if (!hasSafeBoundary(text, match.index, match[0])) {
                continue;
            }
            const candidate = candidateByLabel.get(match[0].toLocaleLowerCase());
            if (candidate) {
                matches.push({ start: match.index, value: match[0], term: candidate.term });
            }
        }
        if (matches.length === 0) {
            return;
        }
        const fragment = document.createDocumentFragment();
        let cursor = 0;
        matches.forEach(({ start, value, term }) => {
            if (start > cursor) {
                fragment.append(document.createTextNode(text.slice(cursor, start)));
            }
            fragment.append(createTermButton(value, term));
            cursor = start + value.length;
        });
        if (cursor < text.length) {
            fragment.append(document.createTextNode(text.slice(cursor)));
        }
        node.replaceWith(fragment);
    };

    const highlightGlossaryTerms = (container) => {
        if (!matcher) {
            return;
        }
        const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
            acceptNode(node) {
                const parent = node.parentElement;
                if (!node.nodeValue?.trim() || !parent || parent.closest(
                    "script, style, input, textarea, button, .translation-zh, "
                    + ".glossary-term, .glossary-popover, [data-glossary-skip]",
                )) {
                    return NodeFilter.FILTER_REJECT;
                }
                return NodeFilter.FILTER_ACCEPT;
            },
        });
        const nodes = [];
        while (walker.nextNode()) {
            nodes.push(walker.currentNode);
        }
        nodes.forEach(highlightTextNode);
    };

    document.querySelectorAll("[data-glossary-highlight]").forEach(highlightGlossaryTerms);

    popoverClose.addEventListener("click", () => closePopover(true));
    document.addEventListener("pointerdown", (event) => {
        if (!popover.hidden && !popover.contains(event.target) && event.target !== activeTrigger) {
            closePopover();
        }
    });
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && !popover.hidden) {
            event.preventDefault();
            closePopover(true);
        }
    });
    window.addEventListener("resize", positionPopover);
    window.addEventListener("scroll", positionPopover, true);

    const page = document.querySelector("[data-glossary-page]");
    if (!(page instanceof HTMLElement)) {
        return;
    }
    const search = page.querySelector("[data-glossary-search]");
    const categoryInput = document.getElementById("glossary-category");
    const cards = [...page.querySelectorAll("[data-glossary-card]")];
    const visibleCount = page.querySelector("[data-glossary-visible-count]");
    const empty = page.querySelector("[data-glossary-empty]");

    const searchable = new Map(glossary.terms.map((term) => [
        term.id,
        [
            term.term,
            ...(term.aliases || []),
            term.term_zh,
            term.definition,
            term.definition_zh,
            term.category,
        ].filter(Boolean).join(" ").toLocaleLowerCase(),
    ]));

    const filterCards = () => {
        const query = search instanceof HTMLInputElement
            ? search.value.trim().toLocaleLowerCase()
            : "";
        const category = categoryInput instanceof HTMLInputElement
            ? categoryInput.value
            : "all";
        let count = 0;
        cards.forEach((card) => {
            const term = termsById.get(card.dataset.termId);
            const matchesSearch = !query || searchable.get(card.dataset.termId)?.includes(query);
            const matchesCategory = category === "all" || term?.category === category;
            const visible = Boolean(matchesSearch && matchesCategory);
            card.hidden = !visible;
            count += visible ? 1 : 0;
        });
        if (visibleCount) {
            visibleCount.textContent = String(count);
        }
        if (empty) {
            empty.hidden = count !== 0;
        }
    };

    search?.addEventListener("input", filterCards);
    page.addEventListener("pickerchange", filterCards);
    page.querySelectorAll("[data-glossary-reveal]").forEach((button) => {
        button.addEventListener("click", () => {
            const controls = button.getAttribute("aria-controls");
            const detail = controls ? document.getElementById(controls) : null;
            if (!(detail instanceof HTMLElement)) {
                return;
            }
            const revealed = detail.hidden;
            detail.hidden = !revealed;
            button.setAttribute("aria-expanded", String(revealed));
            button.textContent = revealed ? "隐藏中文" : "显示中文";
        });
    });
    filterCards();

    window.GlossaryUI = { highlightGlossaryTerms };
})();
