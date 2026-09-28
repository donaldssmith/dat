// ==UserScript==
// @name         Digi4School Downloader (The Zenith Pill - Ultimate V13)
// @namespace    https://digi4school.at/
// @version      14.0
// @description  Hugging Face primary distribution with GitHub fallback, Chinese UX, and safer dual-rail deployment compatibility.
// @match        *://*.digi4school.at/*
// @grant        GM_download
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_xmlhttpRequest
// @require      https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js
// @require      https://cdnjs.cloudflare.com/ajax/libs/pdf-lib/1.17.1/pdf-lib.min.js
// @run-at       document-idle
// ==/UserScript==

(function () {
    'use strict';

    // ==========================================
    // 1. CONFIGURATION
    // ==========================================
    const CONFIG = {
        GITHUB_USER: 'donaldssmith',
        GITHUB_REPO: 'dat',
        HF_REPO_ID: 'donaldssmith/dat',
        HF_REPO_TYPE: 'dataset',
        HF_ENABLED: true,
        CDN_BASE: `https://cdn.jsdelivr.net/gh/donaldssmith/dat@main/dist`,
        RAW_BASE: `https://raw.githubusercontent.com/donaldssmith/dat/main/dist`,
        HF_BASE: `https://huggingface.co/datasets/donaldssmith/dat/resolve/main/dist`,
        WANTED_INDEX_NAME: '-1.dat',
        DISCORD_WEBHOOK: 'https://discord.com/api/webhooks/1495760547633954977/gvgRKIJc52q1q9iQEYnNFIIQBPPEHg9cFxa3KAXi-OSKzj3zXbaDpFmOuYSemgDdjKGn',
        HD_SCALE: 2,
        SYNC_TIMEOUT_MS: 3 * 60 * 1000,
        DIRECT_UPLOAD_LIMIT_BYTES: 24 * 1024 * 1024,
        PDF_PART_SIZE_BYTES: 8 * 1024 * 1024,
        UPLOAD_PROVIDER_CHAIN: ['temp.sh', 'litterbox'],
        TEMPSH_API_URL: 'https://temp.sh/upload',
        LITTERBOX_API_URL: 'https://litterbox.catbox.moe/resources/internals/api.php',
        LITTERBOX_TIME: '72h',
        ZEROX0_API_URL: 'https://0x0.st',
        ZEROX0_USE_SECRET: true,
        CATBOX_API_URL: 'https://catbox.moe/user/api.php',
        CATBOX_USERHASH: '',
        AUTO_REPORT_MISSING_BOOKS: true,
        AUTO_REPORT_COOLDOWN_MS: 14 * 24 * 60 * 60 * 1000,
        INVENTORY_SIGNAL_INTERVAL_MS: 1800,
        INVENTORY_SIGNAL_RETRY_MS: 8000,
        REPORTER_NAME: '',
        // 填写后优先直接提交到 F4S 后端；留空则继续使用 Discord 通知。
        BACKEND_JOB_URL: '',
        BACKEND_API_KEY: '',
        PENDING_REGISTRY_PROVIDER: 'none',
        PENDING_REGISTRY_URL: '',
        PENDING_REGISTRY_READ_KEY: '',
        PENDING_REGISTRY_WRITE_KEY: '',
        PENDING_TTL_HOURS: 168
    };

    function getDistributionSources() {
        const refs = [];
        if (CONFIG.HF_ENABLED && CONFIG.HF_BASE) {
            refs.push({
                key: 'huggingface',
                label: 'Hugging Face',
                base: String(CONFIG.HF_BASE).replace(/\/$/, '')
            });
        }
        refs.push(
            {
                key: 'github_cdn',
                label: 'GitHub CDN',
                base: String(CONFIG.CDN_BASE).replace(/\/$/, '')
            },
            {
                key: 'github_raw',
                label: 'GitHub Raw',
                base: String(CONFIG.RAW_BASE).replace(/\/$/, '')
            }
        );
        return refs;
    }

    function buildDistributionRefs(relativePath, withCacheBust = false) {
        const cleanPath = String(relativePath || '').replace(/^\/+/, '');
        const suffix = withCacheBust ? `${cleanPath.includes('?') ? '&' : '?'}t=${Date.now()}` : '';
        return getDistributionSources().map((source) => ({
            ...source,
            url: `${source.base}/${cleanPath}${suffix}`
        }));
    }

    // ==========================================
    // 2. ROUTING & STATE HELPERS
    // ==========================================
    const isOverview = window.location.pathname.includes('/overview') || window.location.pathname.includes('/meine-buecher');
    const isEbook = window.location.pathname.includes('/ebook');

    function parseLocation() {
        const parts = window.location.pathname.split('/').filter(Boolean);
        return {
            bookId: isEbook ? (parts[1] || '') : '',
            volume: isEbook ? (parts[2] || '') : ''
        };
    }
    const { bookId, volume } = parseLocation();

    function isEbookPlus(titleStr) {
        return /e-book\+/i.test(titleStr) || /ebook\+/i.test(titleStr);
    }

    function normalizeDisplayTitle(rawTitle, fallback = '') {
        let title = String(rawTitle || '').replace(/\s+/g, ' ').trim();
        if (!title) title = fallback;
        title = title.replace(/^Buchcover\s+/i, '');
        title = title.replace(/^教材封面\s*/i, '');
        title = title.replace(/\s*(?:Schülerbuch|Schulbuch|Buch)?\s*(?:und|mit)?\s*\(?\s*(?:E-BOOK\+?|E-Book\+?)\s*\)?\s*$/i, '');
        title = title.replace(/[\s,.;:+\-，。；：！!？?、]+$/g, '');
        title = title.replace(/\s*,\s*$/g, '');
        title = title.replace(/\s{2,}/g, ' ').trim();
        return title || fallback;
    }

    function extractRawTitleFromNode(node, fallbackId) {
        let possibleTitle = node.title || node.alt || '';
        if (!possibleTitle) {
            let rootNode = node;
            while (rootNode.parentNode && rootNode.tagName !== 'APP-BOOK-LIST-ENTRY' && rootNode.tagName !== 'ION-CARD' && rootNode.tagName !== 'A') {
                rootNode = rootNode.parentNode;
                if (rootNode.host) rootNode = rootNode.host;
            }
            if (rootNode) {
                possibleTitle = (rootNode.innerText || rootNode.textContent || '').replace(/\s+/g, ' ').trim();
            }
        }
        return String(possibleTitle || `教材编号 ${fallbackId}`).trim();
    }

    function getThumbnailEntries() {
        try {
            const nodes = Array.from(document.querySelectorAll('#thumbnailPanel a[id^="thumb"]'));
            return nodes.map((node) => {
                const labelNode = node.querySelector('.thumbnailPage');
                const rawLabel = labelNode ? labelNode.textContent : '';
                const label = String(rawLabel || '').trim();
                const href = node.getAttribute('href') || '';
                const sourceFromHref = parseInt(new URL(href, window.location.href).searchParams.get('page'), 10);
                const sourceFromId = parseInt(String(node.id || '').replace(/^thumb/i, ''), 10);
                const sourcePage = !isNaN(sourceFromHref) ? sourceFromHref : sourceFromId;
                return {
                    label,
                    sourcePage
                };
            }).filter((entry) => entry.label && !isNaN(entry.sourcePage));
        } catch (e) {
            return [];
        }
    }

    function getPageLabels() {
        try {
            const meta = document.querySelector('meta[name="pageLabels"]');
            if (meta && meta.content) {
                return meta.content
                    .split(',')
                    .map(label => String(label || '').trim())
                    .filter(Boolean);
            }
            return getThumbnailEntries().map((entry) => entry.label);
        } catch (e) {
            return [];
        }
    }

    function isDisplayPageLabel(label) {
        return /^[0-9]+$/.test(String(label || '').trim());
    }

    function getLastDisplayPageLabel() {
        const labels = getPageLabels();
        for (let i = labels.length - 1; i >= 0; i--) {
            if (isDisplayPageLabel(labels[i])) return labels[i];
        }
        return '';
    }

    function getLeadingNonDisplayCount() {
        const labels = getPageLabels();
        let count = 0;
        for (const label of labels) {
            if (isDisplayPageLabel(label)) break;
            count += 1;
        }
        return count;
    }

    function getTotalSourcePageCount() {
        const thumbnailEntries = getThumbnailEntries();
        if (thumbnailEntries.length > 0) return thumbnailEntries.length;
        return getPageLabels().length;
    }

    function getContributionBoundaryLabels() {
        const labels = getPageLabels();
        return {
            first: labels[0] || '1',
            last: labels[labels.length - 1] || String(getTotalSourcePageCount() || 1)
        };
    }

    function getSourcePageFromLabel(targetLabel) {
        const label = String(targetLabel || '').trim();
        if (!label) return null;

        const thumbnailEntries = getThumbnailEntries();
        if (thumbnailEntries.length > 0) {
            const hit = thumbnailEntries.find((entry) => entry.label === label);
            if (hit) return hit.sourcePage;
        }

        const labels = getPageLabels();
        const idx = labels.indexOf(label);
        if (idx !== -1) {
            const leading = getLeadingNonDisplayCount();
            if (idx >= leading) return idx - leading + 1;
            return idx + 1;
        }

        return null;
    }

    function getDisplayLabelFromSourcePage(sourcePage) {
        const page = parseInt(sourcePage, 10);
        if (isNaN(page)) return '';

        const thumbnailEntries = getThumbnailEntries();
        const hit = thumbnailEntries.find((entry) => entry.sourcePage === page);
        if (hit) return hit.label;

        const labels = getPageLabels();
        return labels[page - 1] || '';
    }

    function normalizeOutlineText(value) {
        return String(value || '').replace(/\s+/g, ' ').trim();
    }

    function randomToken(length = 8) {
        const alphabet = 'abcdefghijklmnopqrstuvwxyz0123456789';
        let out = '';
        for (let i = 0; i < length; i++) {
            out += alphabet[Math.floor(Math.random() * alphabet.length)];
        }
        return out;
    }

    function formatDurationMs(ms) {
        const safeMs = Math.max(0, parseInt(ms, 10) || 0);
        const totalSeconds = Math.max(1, Math.round(safeMs / 1000));
        const minutes = Math.floor(totalSeconds / 60);
        const seconds = totalSeconds % 60;
        if (minutes <= 0) return `${totalSeconds} 秒`;
        if (seconds <= 0) return `${minutes} 分钟`;
        return `${minutes} 分 ${seconds} 秒`;
    }

    function getReporterIdentity() {
        let installId = GM_getValue('f4s_reporter_install_id', '');
        if (!installId) {
            installId = `f4s-${randomToken(8)}`;
            GM_setValue('f4s_reporter_install_id', installId);
        }
        const language = (navigator.language || '').trim();
        let timezone = '';
        try {
            timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
        } catch (e) { }
        return {
            installId,
            name: String(CONFIG.REPORTER_NAME || '').trim(),
            language,
            timezone
        };
    }

    function getOutlineEntries() {
        try {
            const root = document.querySelector('#outlinePanel');
            if (!root) return [];

            const nodes = Array.from(root.querySelectorAll('li[data-page]'));
            const entries = nodes.map((node) => {
                const sourcePage = parseInt(node.getAttribute('data-page'), 10);
                if (isNaN(sourcePage)) return null;

                const directText = Array.from(node.childNodes || [])
                    .filter((child) => child.nodeType === Node.TEXT_NODE)
                    .map((child) => child.textContent)
                    .join(' ');
                const fallbackText = node.textContent || '';
                const title = normalizeOutlineText(directText) || normalizeOutlineText(fallbackText);
                if (!title) return null;

                const titleAttr = normalizeOutlineText(node.getAttribute('title'));
                const displayLabelFromTitle = titleAttr.replace(/^Seite\s+/i, '').trim();
                const displayLabel = getDisplayLabelFromSourcePage(sourcePage) || displayLabelFromTitle;

                let level = 1;
                let parent = node.parentElement;
                while (parent && parent !== root) {
                    if (parent.tagName === 'LI' && parent.hasAttribute('data-page')) level += 1;
                    parent = parent.parentElement;
                }

                return {
                    title,
                    sourcePage,
                    displayLabel: displayLabel || '',
                    level
                };
            }).filter(Boolean);

            return entries;
        } catch (e) {
            return [];
        }
    }

    function buildBookMetadataPayload(title) {
        const pageMap = getThumbnailEntries().map((entry) => ({
            label: entry.label,
            sourcePage: entry.sourcePage
        }));
        const pageLabels = getPageLabels();
        const outline = getOutlineEntries();

        return {
            version: 1,
            kind: 'f4s-book-meta',
            bookId,
            volume: volume || '',
            title: normalizeDisplayTitle(title, bookId),
            generatedAt: new Date().toISOString(),
            pageLabels,
            pageMap,
            sourcePageCount: pageMap.length || pageLabels.length || 0,
            displayPageCount: pageLabels.filter(isDisplayPageLabel).length,
            outlineStatus: outline.length > 0 ? 'present' : 'no outline',
            outlineMessage: outline.length > 0 ? 'outline present' : 'no outline',
            outline
        };
    }

    async function fetchRemoteBookMetadataPayload() {
        const refs = buildDistributionRefs(`${bookId}/0.dat`, true);
        for (const ref of refs) {
            try {
                const text = await gmFetch(ref.url, 'text');
                const parsed = JSON.parse(text);
                if (parsed && typeof parsed === 'object') return parsed;
            } catch (e) { }
        }
        return null;
    }

    async function resolveBookMetadataPayload(title) {
        const remote = await fetchRemoteBookMetadataPayload();
        if (remote && remote.kind === 'f4s-book-meta') return remote;
        return buildBookMetadataPayload(title);
    }

    function getOutlineEntriesForRange(metaPayload, sourceFrom, sourceTo) {
        const outline = Array.isArray(metaPayload?.outline) ? metaPayload.outline : [];
        return outline
            .map((entry) => {
                const sourcePage = parseInt(entry?.sourcePage, 10);
                const level = Math.max(1, parseInt(entry?.level, 10) || 1);
                const title = normalizeOutlineText(entry?.title);
                if (!title || isNaN(sourcePage)) return null;
                if (sourcePage < sourceFrom || sourcePage > sourceTo) return null;
                return {
                    title,
                    level,
                    pageIndex: sourcePage - sourceFrom + 1
                };
            })
            .filter(Boolean);
    }

    function buildOutlineHierarchy(entries) {
        const root = { children: [] };
        const stack = [root];

        for (const entry of entries) {
            const node = { ...entry, children: [] };
            let targetLevel = Math.max(1, entry.level || 1);

            while (stack.length - 1 > targetLevel - 1) stack.pop();
            while (stack.length - 1 < targetLevel - 1) {
                const parent = stack[stack.length - 1];
                const fallback = parent.children[parent.children.length - 1];
                if (!fallback) break;
                stack.push(fallback);
            }

            const parent = stack[stack.length - 1] || root;
            parent.children.push(node);
            stack.length = Math.min(stack.length, targetLevel);
            stack.push(node);
        }

        return root.children;
    }

    function addOutlineNodesToPdf(pdfDoc, nodes, parentRef, pageRefs) {
        const { PDFName, PDFHexString, PDFNumber } = window.PDFLib;
        const context = pdfDoc.context;
        const refs = nodes.map(() => context.register(context.obj({})));
        let totalNodes = 0;

        for (let index = 0; index < nodes.length; index++) {
            const node = nodes[index];
            const pageRef = pageRefs[node.pageIndex - 1];
            if (!pageRef) continue;

            let childFirst = null;
            let childLast = null;
            let childCount = 0;
            if (node.children && node.children.length) {
                const childResult = addOutlineNodesToPdf(pdfDoc, node.children, refs[index], pageRefs);
                childFirst = childResult.firstRef;
                childLast = childResult.lastRef;
                childCount = childResult.totalNodes;
            }

            const dict = context.obj({
                Title: PDFHexString.fromText(node.title),
                Parent: parentRef,
                Dest: context.obj([pageRef, PDFName.of('Fit')])
            });

            if (index > 0) dict.set(PDFName.of('Prev'), refs[index - 1]);
            if (index < refs.length - 1) dict.set(PDFName.of('Next'), refs[index + 1]);
            if (childFirst && childLast) {
                dict.set(PDFName.of('First'), childFirst);
                dict.set(PDFName.of('Last'), childLast);
                dict.set(PDFName.of('Count'), PDFNumber.of(childCount));
            }

            context.assign(refs[index], dict);
            totalNodes += 1 + childCount;
        }

        return {
            firstRef: refs[0] || null,
            lastRef: refs[refs.length - 1] || null,
            totalNodes
        };
    }

    async function attachOutlineBookmarks(pdfDoc, metaPayload, sourceFrom, sourceTo) {
        const outlineEntries = getOutlineEntriesForRange(metaPayload, sourceFrom, sourceTo);
        if (!outlineEntries.length) return 0;

        const pageRefs = pdfDoc.getPages().map((page) => page.ref);
        if (!pageRefs.length) return 0;

        const nodes = buildOutlineHierarchy(outlineEntries);
        if (!nodes.length) return 0;

        const { PDFName, PDFNumber } = window.PDFLib;
        const context = pdfDoc.context;
        const outlinesRef = context.register(context.obj({}));
        const result = addOutlineNodesToPdf(pdfDoc, nodes, outlinesRef, pageRefs);
        if (!result.firstRef || !result.lastRef || result.totalNodes <= 0) return 0;

        const outlineRoot = context.obj({
            Type: PDFName.of('Outlines'),
            First: result.firstRef,
            Last: result.lastRef,
            Count: PDFNumber.of(result.totalNodes)
        });
        context.assign(outlinesRef, outlineRoot);
        pdfDoc.catalog.set(PDFName.of('Outlines'), outlinesRef);
        pdfDoc.catalog.set(PDFName.of('PageMode'), PDFName.of('UseOutlines'));
        return result.totalNodes;
    }

    async function uploadTransientBlob(blob, filename) {
        async function postMultipart(url, fields, fileFieldName, fileBlob, fileName, label) {
            const formData = new FormData();
            for (const [key, value] of Object.entries(fields || {})) {
                if (value === undefined || value === null) continue;
                formData.append(String(key), String(value));
            }
            formData.append(String(fileFieldName), fileBlob, fileName || 'upload.bin');

            return new Promise((resolve, reject) => {
                GM_xmlhttpRequest({
                    method: "POST",
                    url,
                    data: formData,
                    onload: (res) => {
                        const raw = String(res.responseText || res.response || '').trim();
                        if (res.status < 200 || res.status >= 300) {
                            return reject(new Error(`${label} 返回 HTTP ${res.status}${raw ? `: ${raw.slice(0, 160)}` : ''}`));
                        }
                        resolve(raw);
                    },
                    onerror: () => reject(new Error(`${label} 网络失败`))
                });
            });
        }

        async function uploadTo0x0() {
            const fields = {};
            if (CONFIG.ZEROX0_USE_SECRET) fields.secret = '1';
            const raw = await postMultipart(CONFIG.ZEROX0_API_URL, fields, 'file', blob, filename, '0x0');
            if (!raw) {
                throw new Error('0x0 返回空结果');
            }
            if (/^error:/i.test(raw)) {
                throw new Error(raw);
            }
            if (!/^https?:\/\/0x0\.st\//i.test(raw)) {
                throw new Error(`0x0 响应异常: ${raw.slice(0, 160)}`);
            }
            return raw;
        }

        async function uploadToTempSh() {
            const raw = await postMultipart(
                CONFIG.TEMPSH_API_URL,
                {},
                'file',
                blob,
                filename,
                'temp.sh'
            );
            if (!raw || /^error/i.test(raw)) {
                throw new Error(raw || 'temp.sh 返回空结果');
            }
            if (!/^https?:\/\/temp\.sh\//i.test(raw)) {
                throw new Error(`temp.sh 响应异常: ${raw.slice(0, 160)}`);
            }
            return raw;
        }

        async function uploadToLitterbox() {
            const raw = await postMultipart(
                CONFIG.LITTERBOX_API_URL,
                {
                    reqtype: 'fileupload',
                    time: String(CONFIG.LITTERBOX_TIME || '72h')
                },
                'fileToUpload',
                blob,
                filename,
                'litterbox'
            );
            if (!raw || /^error/i.test(raw)) {
                throw new Error(raw || 'litterbox 返回空结果');
            }
            if (!/^https?:\/\/litter\.catbox\.moe\//i.test(raw)) {
                throw new Error(`litterbox 响应异常: ${raw.slice(0, 160)}`);
            }
            return raw;
        }

        async function uploadToCatbox() {
            const fields = {
                reqtype: 'fileupload'
            };
            if (CONFIG.CATBOX_USERHASH) fields.userhash = CONFIG.CATBOX_USERHASH;
            const raw = await postMultipart(CONFIG.CATBOX_API_URL, fields, 'fileToUpload', blob, filename, 'catbox');
            if (!raw || /^error/i.test(raw)) {
                throw new Error(raw || 'catbox 返回空结果');
            }
            if (!/^https?:\/\/files\.catbox\.moe\//i.test(raw)) {
                throw new Error(`catbox 响应异常: ${raw.slice(0, 160)}`);
            }
            return raw;
        }

        function normalizeProviderName(name) {
            const raw = String(name || '').trim().toLowerCase();
            if (!raw) return '';
            if (raw === 'temp.sh' || raw === 'tempsh') return 'temp.sh';
            if (raw === 'litterbox' || raw === 'litter') return 'litterbox';
            if (raw === '0x0' || raw === '0x0.st' || raw === 'zero') return '0x0';
            if (raw === 'catbox') return 'catbox';
            return raw;
        }

        async function uploadByProvider(providerName) {
            if (providerName === 'temp.sh') return uploadToTempSh();
            if (providerName === 'litterbox') return uploadToLitterbox();
            if (providerName === '0x0') return uploadTo0x0();
            if (providerName === 'catbox') return uploadToCatbox();
            throw new Error(`未知上传后端: ${providerName}`);
        }

        const chain = Array.isArray(CONFIG.UPLOAD_PROVIDER_CHAIN)
            ? CONFIG.UPLOAD_PROVIDER_CHAIN
            : String(CONFIG.UPLOAD_PROVIDER_CHAIN || '')
                .split(',')
                .map((item) => item.trim())
                .filter(Boolean);
        const providers = Array.from(new Set(chain.map(normalizeProviderName).filter(Boolean)));
        const failures = [];

        for (const provider of providers) {
            try {
                return await uploadByProvider(provider);
            } catch (error) {
                failures.push(`${provider}: ${error.message || error}`);
                console.warn(`Upload provider failed (${provider}), trying next:`, error);
            }
        }

        throw new Error(`所有上传后端均失败: ${failures.join(' | ')}`);
    }

    async function uploadPdfPayload(blob, filename, title, onProgress = null) {
        function buildPdfPartName(originalName, partIndex) {
            const suffix = `.part${String(partIndex).padStart(3, '0')}`;
            const match = String(originalName).match(/^(.*?)(\.[^.]+)$/);
            if (match) {
                const base = match[1];
                const ext = match[2];
                return `${base}${suffix}${ext}`;
            }
            return `${originalName}${suffix}.pdf`;
        }

        if (blob.size <= CONFIG.DIRECT_UPLOAD_LIMIT_BYTES) {
            const link = await uploadTransientBlob(blob, filename);
            return {
                mode: 'single',
                link,
                label: link,
                parts: []
            };
        }

        const partSize = CONFIG.PDF_PART_SIZE_BYTES;
        const partCount = Math.ceil(blob.size / partSize);
        const parts = [];

        for (let index = 0; index < partCount; index++) {
            const start = index * partSize;
            const end = Math.min(start + partSize, blob.size);
            const partBlob = blob.slice(start, end, 'application/octet-stream');
            const partName = buildPdfPartName(filename, index + 1);
            if (onProgress) onProgress(index + 1, partCount, partName);
            const url = await uploadTransientBlob(partBlob, partName);
            parts.push({
                index: index + 1,
                name: partName,
                size: partBlob.size,
                url
            });
        }

        const manifest = {
            version: 1,
            kind: 'f4s-pdf-parts',
            bookId,
            title,
            filename,
            size: blob.size,
            partSize,
            partCount,
            generatedAt: new Date().toISOString(),
            parts
        };
        const manifestBlob = new Blob([JSON.stringify(manifest, null, 2)], { type: 'application/json' });
        const manifestLink = await uploadTransientBlob(manifestBlob, `${bookId}-pdf.parts.json`);

        return {
            mode: 'parts',
            link: manifestLink,
            label: `manifest: ${manifestLink} (${partCount} parts)`,
            parts
        };
    }

    function buildJobEnvelope(payload) {
        return {
            version: 1,
            protocol: 'F4S_JOB',
            type: payload.type,
            source: 'userscript-v13',
            createdAt: new Date().toISOString(),
            bookId: String(payload.bookId || ''),
            title: normalizeDisplayTitle(payload.title, payload.bookId),
            volume: payload.volume || '',
            sourcePageCount: payload.sourcePageCount || 0,
            outline: {
                status: payload.outlineStatus || 'unknown',
                count: payload.outlineCount || 0
            },
            pdf: payload.pdf || {
                mode: 'none',
                url: '',
                label: 'none'
            },
            meta: {
                url: payload.metaLink || ''
            },
            wanted: !!payload.isWanted,
            reporter: payload.reporter || null
        };
    }

    function buildJobHumanLine(job) {
        if (job.type === 'full_upload') return `Полная выгрузка: ${job.bookId} / ${job.title}`;
        if (job.type === 'meta_upload') return `补书库信息: ${job.bookId} / ${job.title}`;
        if (job.type === 'nudge') return `催维护: ${job.bookId} / ${job.title}`;
        if (job.type === 'inventory_signal') return `缺书线索: ${job.bookId} / ${job.title}`;
        return `F4S job: ${job.bookId} / ${job.title}`;
    }

    async function postJobNotice(payload) {
        const job = buildJobEnvelope(payload);

        if (CONFIG.BACKEND_JOB_URL) {
            try {
                await new Promise((resolve, reject) => {
                    const headers = { 'Content-Type': 'application/json' };
                    if (CONFIG.BACKEND_API_KEY) headers['X-API-Key'] = CONFIG.BACKEND_API_KEY;
                    GM_xmlhttpRequest({
                        method: 'POST',
                        url: CONFIG.BACKEND_JOB_URL,
                        headers,
                        data: JSON.stringify(job),
                        onload: (res) => {
                            if (res.status >= 200 && res.status < 300) resolve();
                            else reject(new Error(`后端任务提交失败: HTTP ${res.status}`));
                        },
                        onerror: () => reject(new Error('后端任务提交失败: 网络异常'))
                    });
                });
                return;
            } catch (backendError) {
                console.warn('F4S backend unavailable, falling back to Discord:', backendError);
            }
        }

        const lines = [
            '**F4S JOB**',
            buildJobHumanLine(job),
            '```json',
            JSON.stringify(job, null, 2),
            '```'
        ];

        return new Promise((resolve, reject) => {
            GM_xmlhttpRequest({
                method: "POST",
                url: CONFIG.DISCORD_WEBHOOK,
                headers: { "Content-Type": "application/json" },
                data: JSON.stringify({ content: lines.join('\n') }),
                onload: (hookRes) => {
                    if (hookRes.status >= 200 && hookRes.status < 300) resolve();
                    else {
                        const err = new Error(`任务通知失败: HTTP ${hookRes.status}`);
                        err.status = hookRes.status;
                        err.responseHeaders = hookRes.responseHeaders || '';
                        reject(err);
                    }
                },
                onerror: () => reject(new Error("任务通知发送失败"))
            });
        });
    }

    const InventorySignalDispatcher = {
        queue: [],
        queuedBookIds: new Set(),
        running: false,

        sleep(ms) {
            return new Promise((resolve) => setTimeout(resolve, ms));
        },

        enqueue(bookIdToReport, titleToReport) {
            if (!CONFIG.AUTO_REPORT_MISSING_BOOKS) return;
            const bookIdStr = String(bookIdToReport || '').trim();
            if (!bookIdStr || this.queuedBookIds.has(bookIdStr)) return;
            this.queuedBookIds.add(bookIdStr);
            this.queue.push({
                bookId: bookIdStr,
                title: titleToReport
            });
            this.kick();
        },

        kick() {
            if (this.running) return;
            this.running = true;
            this.flush().finally(() => {
                this.running = false;
                if (this.queue.length > 0) this.kick();
            });
        },

        async flush() {
            while (this.queue.length > 0) {
                const job = this.queue.shift();
                this.queuedBookIds.delete(job.bookId);
                try {
                    await this.send(job.bookId, job.title);
                    await this.sleep(CONFIG.INVENTORY_SIGNAL_INTERVAL_MS);
                } catch (err) {
                    if (err && err.status === 429) {
                        console.warn(`D4S inventory signal rate-limited for ${job.bookId}, retrying later.`);
                        await this.sleep(CONFIG.INVENTORY_SIGNAL_RETRY_MS);
                        this.enqueue(job.bookId, job.title);
                    } else {
                        console.warn('D4S inventory signal probe failed:', err);
                    }
                }
            }
        },

        async send(bookIdToReport, titleToReport) {
            const cooldownKey = `f4s_missing_signal_${bookIdToReport}`;
            const lastSentAt = parseInt(GM_getValue(cooldownKey, 0), 10) || 0;
            if (Date.now() - lastSentAt < CONFIG.AUTO_REPORT_COOLDOWN_MS) return;

            await postJobNotice({
                type: 'inventory_signal',
                bookId: bookIdToReport,
                title: titleToReport,
                reporter: getReporterIdentity(),
                sourcePageCount: 0,
                outlineStatus: 'unknown',
                outlineCount: 0,
                isWanted: false,
                pdf: {
                    mode: 'none',
                    url: '',
                    label: 'none'
                }
            });

            GM_setValue(cooldownKey, Date.now());
        }
    };

    function reportMissingBookSignal(bookIdToReport, titleToReport) {
        InventorySignalDispatcher.enqueue(bookIdToReport, titleToReport);
    }

    function buildPendingRecord(payload) {
        const now = new Date();
        const expiresAt = new Date(now.getTime() + CONFIG.PENDING_TTL_HOURS * 60 * 60 * 1000);
        return {
            version: 1,
            status: 'pending',
            bookId: payload.bookId,
            title: payload.title,
            volume: payload.volume || '',
            createdAt: now.toISOString(),
            expiresAt: expiresAt.toISOString(),
            pdfLink: payload.pdfLink || '',
            metaLink: payload.metaLink || '',
            outlineStatus: payload.outlineStatus || 'unknown',
            outlineCount: payload.outlineCount || 0,
            sourcePageCount: payload.sourcePageCount || 0
        };
    }

    async function registryJsonRequest(method, url, body = null, headers = {}) {
        return new Promise((resolve, reject) => {
            GM_xmlhttpRequest({
                method,
                url,
                headers,
                data: body ? JSON.stringify(body) : undefined,
                onload: (res) => {
                    try {
                        const raw = typeof res.responseText === 'string' && res.responseText
                            ? res.responseText
                            : (typeof res.response === 'string' ? res.response : JSON.stringify(res.response || {}));
                        const data = raw ? JSON.parse(raw) : {};
                        if (res.status >= 200 && res.status < 300) resolve(data);
                        else reject(new Error(data.message || `registry http ${res.status}`));
                    } catch (e) {
                        if (res.status >= 200 && res.status < 300) resolve({});
                        else reject(new Error(`registry parse ${res.status}`));
                    }
                },
                onerror: () => reject(new Error('registry network failed'))
            });
        });
    }

    const PendingRegistry = {
        isEnabled() {
            return CONFIG.PENDING_REGISTRY_PROVIDER !== 'none' && !!CONFIG.PENDING_REGISTRY_URL;
        },

        getReadHeaders() {
            const headers = {};
            if (CONFIG.PENDING_REGISTRY_READ_KEY) headers['x-read-key'] = CONFIG.PENDING_REGISTRY_READ_KEY;
            return headers;
        },

        getWriteHeaders() {
            const headers = { 'Content-Type': 'application/json' };
            if (CONFIG.PENDING_REGISTRY_WRITE_KEY) headers['x-write-key'] = CONFIG.PENDING_REGISTRY_WRITE_KEY;
            return headers;
        },

        async fetchRecord(targetBookId) {
            if (!this.isEnabled()) return null;

            if (CONFIG.PENDING_REGISTRY_PROVIDER === 'google_apps_script') {
                const url = `${CONFIG.PENDING_REGISTRY_URL}?bookId=${encodeURIComponent(targetBookId)}`;
                const data = await registryJsonRequest('GET', url, null, this.getReadHeaders());
                return data && data.status === 'pending' ? data : null;
            }

            if (CONFIG.PENDING_REGISTRY_PROVIDER === 'cloudflare_worker') {
                const url = `${CONFIG.PENDING_REGISTRY_URL.replace(/\/$/, '')}/pending/${encodeURIComponent(targetBookId)}`;
                const data = await registryJsonRequest('GET', url, null, this.getReadHeaders());
                return data && data.status === 'pending' ? data : null;
            }

            return null;
        },

        async publishRecord(payload) {
            if (!this.isEnabled()) return false;
            const record = buildPendingRecord(payload);

            if (CONFIG.PENDING_REGISTRY_PROVIDER === 'google_apps_script') {
                const body = {
                    action: 'upsert_pending',
                    record
                };
                await registryJsonRequest('POST', CONFIG.PENDING_REGISTRY_URL, body, this.getWriteHeaders());
                return true;
            }

            if (CONFIG.PENDING_REGISTRY_PROVIDER === 'cloudflare_worker') {
                const url = `${CONFIG.PENDING_REGISTRY_URL.replace(/\/$/, '')}/pending`;
                await registryJsonRequest('POST', url, record, this.getWriteHeaders());
                return true;
            }

            return false;
        }
    };

    async function gmFetch(url, type = 'text') {
        return new Promise((resolve, reject) => {
            GM_xmlhttpRequest({
                method: "GET",
                url: url,
                responseType: type,
                onload: (res) => {
                    if (res.status >= 200 && res.status < 300) resolve(type === 'arraybuffer' ? res.response : res.responseText);
                    else reject(new Error(`HTTP ${res.status}`));
                },
                onerror: () => reject(new Error("浏览器拦截或网络异常"))
            });
        });
    }

    async function probeCloudStatus(testBookId) {
        const refs = buildDistributionRefs(`${testBookId}/1.dat`, true);
        for (const ref of refs) {
            const isHit = await new Promise((resolve) => {
                GM_xmlhttpRequest({
                    method: "HEAD",
                    url: ref.url,
                    onload: (res) => resolve(res.status === 200),
                    onerror: () => resolve(false)
                });
            });
            if (isHit) return true;
        }
        return false;
    }

    async function probeMetadataStatus(testBookId) {
        const refs = buildDistributionRefs(`${testBookId}/0.dat`, true);
        for (const ref of refs) {
            const isHit = await new Promise((resolve) => {
                GM_xmlhttpRequest({
                    method: "HEAD",
                    url: ref.url,
                    onload: (res) => resolve(res.status === 200),
                    onerror: () => resolve(false)
                });
            });
            if (isHit) return true;
        }
        return false;
    }

    const WantedRegistry = {
        loaded: false,
        entries: new Map(),

        normalizeEntries(raw) {
            const list = Array.isArray(raw)
                ? raw
                : Array.isArray(raw?.wantedBooks)
                    ? raw.wantedBooks
                    : Array.isArray(raw?.books)
                        ? raw.books
                        : [];

            const map = new Map();
            for (const item of list) {
                if (typeof item === 'string' || typeof item === 'number') {
                    const bookId = String(item).trim();
                    if (bookId) map.set(bookId, { bookId, title: '', note: '', priority: 'normal' });
                    continue;
                }

                if (!item || typeof item !== 'object') continue;
                const bookId = String(item.bookId || item.id || '').trim();
                if (!bookId) continue;
                map.set(bookId, {
                    bookId,
                    title: String(item.title || '').trim(),
                    note: String(item.note || '').trim(),
                    priority: String(item.priority || 'normal').trim()
                });
            }
            return map;
        },

        async load(force = false) {
            if (this.loaded && !force) return this.entries;

            const refs = buildDistributionRefs(CONFIG.WANTED_INDEX_NAME, true);

            let parsed = { wantedBooks: [] };
            for (const ref of refs) {
                try {
                    const text = await gmFetch(ref.url, 'text');
                    parsed = JSON.parse(text);
                    break;
                } catch (e) { }
            }

            this.entries = this.normalizeEntries(parsed);
            this.loaded = true;
            return this.entries;
        },

        get(bookId) {
            return this.entries.get(String(bookId).trim()) || null;
        },

        has(bookId) {
            return !!this.get(bookId);
        }
    };

    // ==========================================
    // 3. TASK QUEUE MANAGER 
    // ==========================================
    const TaskManager = {
        getKey: (id) => `d4s_task_${id}`,
        set: (id, state, message = '') => {
            GM_setValue(TaskManager.getKey(id), { state, message, timestamp: Date.now() });
        },
        get: (id) => {
            return GM_getValue(TaskManager.getKey(id), null);
        },
        clear: (id) => {
            GM_setValue(TaskManager.getKey(id), null);
        }
    };

    // ==========================================
    // 4. UI ENGINE 
    // ==========================================
    const UIEngine = {
        initCSS: () => {
            if (document.getElementById('d4s-style')) return true;
            const css = document.createElement('style');
            css.id = 'd4s-style';
            css.textContent = `
                :root { 
                    --pill-bg: rgba(255, 255, 255, 0.7); --pill-blur: blur(24px) saturate(180%); --pill-border: rgba(0, 0, 0, 0.05); --pill-bevel: inset 0 1px 1px rgba(255, 255, 255, 0.8); --pill-shadow: 0 4px 12px -2px rgba(0,0,0,0.04), 0 12px 32px -4px rgba(0,0,0,0.06); 
                    --text-main: #09090b; --text-dim: #71717a; --action-bg: #09090b; --action-fg: #ffffff; --progress-fill: #e4e4e7;
                    --panel-bg: rgba(255, 255, 255, 0.85); --success-col: #10b981; --warn-col: #f59e0b; --err-col: #ef4444; font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
                }
                @media (prefers-color-scheme: dark) { 
                    :root { 
                        --pill-bg: rgba(24, 24, 27, 0.7); --pill-border: rgba(255, 255, 255, 0.08); --pill-bevel: inset 0 1px 1px rgba(255, 255, 255, 0.1); --pill-shadow: 0 4px 12px -2px rgba(0,0,0,0.2), 0 12px 32px -4px rgba(0,0,0,0.4); 
                        --text-main: #fafafa; --text-dim: #a1a1aa; --action-bg: #fafafa; --action-fg: #09090b; --progress-fill: #3f3f46; --panel-bg: rgba(24, 24, 27, 0.85);
                    } 
                }
                #d4s-container { position: fixed; top: 24px; right: 24px; z-index: 2147483647; display: flex; flex-direction: column; align-items: flex-end; gap: 8px; }
                #d4s-pill { display: flex; align-items: center; gap: 12px; min-width: 250px; justify-content: space-between; background: var(--pill-bg); backdrop-filter: var(--pill-blur); -webkit-backdrop-filter: var(--pill-blur); border: 1px solid var(--pill-border); border-radius: 99px; padding: 8px 8px 8px 18px; box-shadow: var(--pill-shadow), var(--pill-bevel); color: var(--text-main); user-select: none; transition: box-shadow 0.3s ease; }
                .drag-handle { cursor: move; flex-grow: 1; display:flex; align-items:center; } 
                .book-title { font-size: 13.5px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 140px; }
                .val-group { display: flex; align-items: center; gap: 4px; border-left: 1px solid var(--pill-border); padding-left: 12px; margin-left: auto; } 
                .val-group input { width: 3.8ch; text-align: center; font-size: 13.5px; font-weight: 600; font-variant-numeric: tabular-nums; background: transparent; color: var(--text-main); border: none; outline: none; padding: 4px 2px; border-radius: 6px; }
                .val-group input:hover, .val-group input:focus { background: rgba(113, 113, 122, 0.1); }
                
                .action-btn { position: relative; width: 34px; height: 34px; border-radius: 50%; background: var(--action-bg); color: var(--action-fg); border: none; cursor: pointer; display: flex; align-items: center; justify-content: center; flex-shrink: 0; overflow: hidden; transition: transform 0.2s cubic-bezier(0.34, 1.56, 0.64, 1), background 0.3s, box-shadow 0.3s; }
                .action-btn:active:not(:disabled) { transform: scale(0.9); }
                .action-btn:disabled { opacity: 0.6; cursor: not-allowed; }
                .action-icon { width: 14px; height: 14px; fill: none; stroke: currentColor; stroke-width: 2.2; stroke-linecap: round; stroke-linejoin: round; z-index: 2; }
                .action-progress { position: absolute; bottom: 0; left: 0; width: 100%; height: 0%; background: var(--progress-fill); z-index: 1; transition: height 0.1s linear; }
                #d4s-statusbar { width: 100%; max-width: 340px; background: var(--panel-bg); backdrop-filter: var(--pill-blur); -webkit-backdrop-filter: var(--pill-blur); border: 1px solid var(--pill-border); border-radius: 14px; box-shadow: var(--pill-shadow), var(--pill-bevel); padding: 10px 14px; display: none; color: var(--text-main); }
                #d4s-statusbar.is-open { display: block; animation: dropIn 0.25s cubic-bezier(0.2, 0.8, 0.2, 1) forwards; }
                .status-title { font-size: 12px; font-weight: 700; }
                .status-copy { font-size: 11px; color: var(--text-dim); margin-top: 2px; line-height: 1.35; }
                .status-actions { display: none; margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--pill-border); }
                .status-actions.is-open { display: flex; justify-content: flex-end; gap: 8px; flex-wrap: wrap; }
                .status-action-btn { border: 1px solid var(--pill-border); background: rgba(113, 113, 122, 0.08); color: var(--text-main); border-radius: 999px; padding: 6px 12px; font-size: 11px; font-weight: 600; cursor: pointer; transition: background 0.2s ease, transform 0.2s ease; }
                .status-action-btn:hover:not(:disabled) { background: rgba(113, 113, 122, 0.14); }
                .status-action-btn:active:not(:disabled) { transform: scale(0.97); }
                .status-action-btn:disabled { opacity: 0.55; cursor: not-allowed; }
                .status-action-btn.is-primary { background: rgba(9, 9, 11, 0.9); color: var(--action-fg); border-color: transparent; }
                .status-action-btn.is-primary:hover:not(:disabled) { background: rgba(9, 9, 11, 1); }
                
                #d4s-expand-btn { width: 24px; height: 34px; border: none; background: transparent; cursor: pointer; display: flex; align-items: center; justify-content: center; color: var(--text-dim); margin-left: -6px; border-left: 1px solid var(--pill-border); }
                #d4s-expand-btn svg { width: 14px; height: 14px; fill: none; stroke: currentColor; stroke-width: 2.5; stroke-linecap: round; stroke-linejoin: round; transition: transform 0.3s ease; }
                #d4s-expand-btn.is-open svg { transform: rotate(180deg); }
                #d4s-panel { width: 340px; background: var(--panel-bg); backdrop-filter: var(--pill-blur); -webkit-backdrop-filter: var(--pill-blur); border: 1px solid var(--pill-border); border-radius: 16px; box-shadow: var(--pill-shadow), var(--pill-bevel); padding: 16px; display: none; transform-origin: top right; }
                #d4s-panel.is-open { display: block; animation: dropIn 0.3s cubic-bezier(0.2, 0.8, 0.2, 1) forwards; }
                @keyframes dropIn { from { opacity: 0; transform: scale(0.95) translateY(-10px); } to { opacity: 1; transform: scale(1) translateY(0); } }
                .panel-title { font-size: 13px; font-weight: 700; color: var(--text-dim); margin-bottom: 8px; border-bottom: 1px solid var(--pill-border); padding-bottom: 8px; display:flex; justify-content:space-between; }
                .task-list { max-height: 280px; overflow-y: auto; display:flex; flex-direction: column; gap: 6px; }
                .task-item { display:flex; align-items:center; justify-content:space-between; background: rgba(113,113,122,0.06); padding: 10px 12px; border-radius: 8px; }
                .task-info { display: flex; flex-direction: column; gap: 4px; overflow:hidden; }
                .task-id { font-size: 13px; font-weight: 600; color: var(--text-main); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 180px; }
                .task-sub { font-size: 11px; color: var(--text-dim); }
                .task-btn { background: var(--action-bg); color: var(--action-fg); border: none; padding: 6px 14px; border-radius: 14px; font-size: 11px; cursor: pointer; font-weight:600; flex-shrink: 0; }
                .task-btn:disabled { opacity: 0.5; cursor: default; }
                
                .d4s-ui-download { background: var(--text-main); color: var(--action-fg); }
                .d4s-ui-contribute { background: rgba(113, 113, 122, 0.12); color: var(--text-main); border: 1px solid var(--pill-border); }
                .d4s-ui-disabled { background: rgba(113, 113, 122, 0.1); color: var(--text-dim); border: none; }
                
                .status-running { color: var(--warn-col); font-weight: 600; }
                .status-failed { color: var(--err-col); font-weight: 600; }
                .status-done { color: var(--success-col); font-weight: 600; }
            `;
            const target = document.head || document.documentElement || document.body;
            if (!target) return false;
            target.appendChild(css);
            return true;
        },
        createContainer: () => {
            const c = document.createElement('div');
            c.id = 'd4s-container';
            return c;
        },
        applyDrag: (container, dragHandle) => {
            let drag = false, ox = 0, oy = 0;
            dragHandle.addEventListener('mousedown', e => {
                drag = true;
                const r = container.getBoundingClientRect();
                ox = e.clientX - r.left; oy = e.clientY - r.top;
            });
            document.addEventListener('mousemove', e => {
                if (!drag) return;
                container.style.right = 'auto';
                container.style.left = `${e.clientX - ox}px`;
                container.style.top = `${e.clientY - oy}px`;
            });
            document.addEventListener('mouseup', () => { drag = false; });
        }
    };

    // ==========================================
    // 5. OVERVIEW MANAGER (Task Dispatcher)
    // ==========================================
    class OverviewManager {
        constructor(container) {
            this.container = container;
            this.discovered = new Map(); // bookId -> { title }
            this.cacheMisses = new Map(); // bookId -> { title }
            this.isUIOpen = false;
            this.isSweeping = false;
            this.scanBooks = null;
        }

        async init() {
            await WantedRegistry.load();
            this.render();
            this.setupObserver();
            setTimeout(() => this.startShelfSweep(), 1200);
            setInterval(() => this.updateStateMapping(), 1500);
            setInterval(() => this.reprobeMisses(), 25000);
        }

        render() {
            this.container.innerHTML = `
                <div id="d4s-pill">
                    <div class="drag-handle"><span class="book-title">书架征集</span></div>
                    <div class="val-group"><span style="font-size:12px; font-weight:600; color:var(--text-main);" id="d4s-ov-stat">正在整理</span></div>
                    <button id="d4s-expand-btn"><svg viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"></polyline></svg></button>
                </div>
                <div id="d4s-panel">
            <div class="panel-title"><span>你书架里命中的征集书</span><span id="d4s-ov-count">0</span></div>
                    <div id="d4s-task-list" class="task-list"><span style="font-size:12px; color:var(--text-dim); padding: 8px;">正在核对当前书架和征集清单...</span></div>
                </div>
            `;
            const expandBtn = this.container.querySelector('#d4s-expand-btn');
            const panel = this.container.querySelector('#d4s-panel');
            expandBtn.onclick = () => {
                this.isUIOpen = !this.isUIOpen;
                expandBtn.classList.toggle('is-open', this.isUIOpen);
                panel.classList.toggle('is-open', this.isUIOpen);
            };
            UIEngine.applyDrag(this.container, this.container.querySelector('.drag-handle'));
        }

        extractNodeTitle(node, fallbackId) {
            const rawTitle = extractRawTitleFromNode(node, fallbackId);
            return normalizeDisplayTitle(rawTitle, `教材编号 ${fallbackId}`);
        }

        setupObserver() {
            const scan = () => {
                const newBooks = new Map();

                const self = this;
                function dig(node) {
                    if (!node) return;
                    if (node.nodeType === Node.ELEMENT_NODE) {
                        let src = node.getAttribute('src');
                        if (node.tagName === 'ION-IMG' && node.src) src = node.src;
                        if (src) {
                            const match = src.match(/thumb\/(\d+)/);
                            if (match) {
                                const id = match[1];
                                const rawTitle = extractRawTitleFromNode(node, id);
                                const title = normalizeDisplayTitle(rawTitle, `教材编号 ${id}`);
                                if (!isEbookPlus(rawTitle)) { // 排除 E-Book+ 系列
                                    newBooks.set(id, title);
                                }
                            }
                        }
                    }
                    if (node.shadowRoot) dig(node.shadowRoot);
                    let child = node.firstElementChild || (node.shadowRoot && node.shadowRoot.firstElementChild) || node.firstChild;
                    while (child) { dig(child); child = child.nextElementSibling || child.nextSibling; }
                }
                dig(document.body);

                newBooks.forEach((title, id) => {
                    if (!this.discovered.has(id)) {
                        this.discovered.set(id, { title });
                        if (WantedRegistry.has(id)) {
                            const wanted = WantedRegistry.get(id);
                            const displayTitle = normalizeDisplayTitle(wanted?.title || title, title);
                            this.checkCloud(id, displayTitle);
                        } else {
                            probeCloudStatus(id)
                                .then((isHit) => {
                                    if (!isHit) return reportMissingBookSignal(id, title);
                                    return null;
                                })
                                .catch((signalErr) => {
                                    console.warn('D4S inventory signal probe failed:', signalErr);
                                });
                        }
                    }
                });
            };
            this.scanBooks = scan;
            scan();
            const obs = new MutationObserver(scan);
            obs.observe(document.body, { childList: true, subtree: true });
        }

        isScrollableElement(element) {
            if (!element) return false;
            if (element === document.body || element === document.documentElement || element === document.scrollingElement) {
                const scrollEl = document.scrollingElement || document.documentElement;
                return !!scrollEl && scrollEl.scrollHeight > scrollEl.clientHeight + 200;
            }
            const style = window.getComputedStyle(element);
            const overflowY = style.overflowY || '';
            const allowsScroll = overflowY === 'auto' || overflowY === 'scroll' || overflowY === 'overlay';
            return allowsScroll && element.scrollHeight > element.clientHeight + 120;
        }

        collectSweepTargets() {
            const targets = [];
            const pushTarget = (element) => {
                if (!element) return;
                if (!this.isScrollableElement(element)) return;
                if (targets.includes(element)) return;
                targets.push(element);
            };

            pushTarget(document.scrollingElement || document.documentElement);

            const selectorHits = document.querySelectorAll('ion-content, .inner-scroll, main, [role="main"], .scroll-content, .content, .book-list');
            selectorHits.forEach((node) => pushTarget(node));

            const genericHits = Array.from(document.querySelectorAll('div, section, main')).filter((node) => this.isScrollableElement(node));
            genericHits
                .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight))
                .slice(0, 4)
                .forEach((node) => pushTarget(node));

            return targets;
        }

        getScrollTop(element) {
            if (element === document.body || element === document.documentElement || element === document.scrollingElement) {
                const scrollEl = document.scrollingElement || document.documentElement;
                return scrollEl.scrollTop;
            }
            return element.scrollTop;
        }

        setScrollTop(element, top) {
            if (element === document.body || element === document.documentElement || element === document.scrollingElement) {
                const scrollEl = document.scrollingElement || document.documentElement;
                scrollEl.scrollTop = top;
                window.scrollTo(0, top);
                return;
            }
            element.scrollTop = top;
        }

        async sweepElement(element) {
            const startTop = this.getScrollTop(element);
            const maxTop = Math.max(0, (element === document.body || element === document.documentElement || element === document.scrollingElement)
                ? ((document.scrollingElement || document.documentElement).scrollHeight - (document.scrollingElement || document.documentElement).clientHeight)
                : (element.scrollHeight - element.clientHeight));

            if (maxTop <= 0) return;

            const step = Math.max(360, Math.floor(((element === document.body || element === document.documentElement || element === document.scrollingElement)
                ? (document.scrollingElement || document.documentElement).clientHeight
                : element.clientHeight) * 0.85));

            for (let top = 0; top <= maxTop; top += step) {
                this.setScrollTop(element, top);
                if (this.scanBooks) this.scanBooks();
                await new Promise((resolve) => setTimeout(resolve, 220));
            }

            this.setScrollTop(element, maxTop);
            if (this.scanBooks) this.scanBooks();
            await new Promise((resolve) => setTimeout(resolve, 240));

            this.setScrollTop(element, startTop);
            if (this.scanBooks) this.scanBooks();
        }

        async startShelfSweep() {
            if (this.isSweeping) return;
            this.isSweeping = true;
            try {
                const targets = this.collectSweepTargets();
                for (const target of targets) {
                    await this.sweepElement(target);
                }
            } catch (err) {
                console.warn('D4S shelf sweep failed:', err);
            } finally {
                this.isSweeping = false;
            }
        }

        async setHitStatus(id, title, isHit) {
            if (isHit) {
                this.cacheMisses.delete(id);
                TaskManager.clear(id);
            } else {
                this.cacheMisses.set(id, { title });
                if (!WantedRegistry.has(id)) {
                    try {
                        await reportMissingBookSignal(id, title);
                    } catch (signalErr) {
                        console.warn('D4S missing-book signal failed:', signalErr);
                    }
                }
            }
            this.updateStateMapping();
        }

        async checkCloud(id, title) {
            const isHit = await probeCloudStatus(id);
            this.setHitStatus(id, title, isHit);
        }

        async reprobeMisses() {
            for (const [id, data] of Array.from(this.cacheMisses.entries())) {
                const task = TaskManager.get(id);
                if (task && (task.state === 'running' || task.state === 'queued')) continue;
                const isHit = await probeCloudStatus(id);
                if (isHit) this.setHitStatus(id, data.title, true);
            }
        }

        startTask(id) {
            TaskManager.set(id, 'queued', '正在打开书页...');
            const win = window.open(`https://a.digi4school.at/ebook/${id}`, '_blank');
            if (!win) {
                TaskManager.set(id, 'failed', '新窗口被浏览器拦截');
                setTimeout(() => TaskManager.clear(id), 5000);
            }
            this.updateStateMapping();
        }

        updateStateMapping() {
            this.container.querySelector('#d4s-ov-stat').innerText = `${this.cacheMisses.size} 本待响应`;
            this.container.querySelector('#d4s-ov-count').innerText = this.cacheMisses.size;

            const listEl = this.container.querySelector('#d4s-task-list');
            if (this.cacheMisses.size === 0) {
                listEl.innerHTML = '<span style="font-size:12px; color:var(--text-dim); padding:8px;">当前书架里没有命中征集清单的普通教材。直接打开你要看的书即可；如果后面被加入征集清单，这里会自动出现。</span>';
                return;
            }

            listEl.innerHTML = '';
            for (let [id, data] of Array.from(this.cacheMisses.entries())) {
                let task = TaskManager.get(id);
                if (task && task.state === 'done' && Date.now() - (task.timestamp || 0) > CONFIG.SYNC_TIMEOUT_MS) {
                    TaskManager.set(id, 'failed', '等待云端同步超时，可重新打开书页再试。');
                    task = TaskManager.get(id);
                }
                const state = task ? task.state : 'idle';

                let btnTxt = '打开';
                let btnDisabled = '';
                let subClass = 'task-sub';
                let subTxt = '这本书在当前征集清单里。打开后你可以先下载自己要的范围；如果你愿意，再点书页下方的“手动贡献整本”。';

                if (state === 'queued') {
                    btnTxt = '打开中'; btnDisabled = 'disabled'; subTxt = '正在为你打开对应书页...'; subClass += ' status-running';
                } else if (state === 'running') {
                    btnTxt = '处理中'; btnDisabled = 'disabled'; subTxt = task.message; subClass += ' status-running';
                } else if (state === 'failed') {
                    btnTxt = '重试'; subClass += ' status-failed'; subTxt = task.message;
                } else if (state === 'done') {
                    btnTxt = '等待同步'; btnDisabled = 'disabled'; subTxt = '整本已经提交，等仓库同步完成后，这项会自动从书架征集里消失。'; subClass += ' status-done';
                }

                const item = document.createElement('div');
                item.className = 'task-item';
                item.innerHTML = `
                    <div class="task-info" title="${data.title}">
                        <span class="task-id">${data.title}</span>
                        <span class="${subClass}">${subTxt}</span>
                    </div>
                    <button class="task-btn" ${btnDisabled}>${btnTxt}</button>
                `;
                if (!btnDisabled) {
                    item.querySelector('.task-btn').onclick = () => this.startTask(id);
                }
                listEl.appendChild(item);
            }
        }
    }

    // ==========================================
    // 6. EBOOK MANAGER (Task Executor & UI)
    // ==========================================
    class EbookManager {
        constructor(container) {
            this.container = container;
            this.rawTitle = this.extractRawTitle();
            this.title = normalizeDisplayTitle(this.rawTitle, bookId);
            this.totalPages = this.detectPages();
            this.isCloudReady = false;
            this.hasRemoteMeta = false;
            this.isRunning = false;
            this.isPlusBook = isEbookPlus(this.rawTitle);
            this.isWanted = false;
            this.runMode = 'idle';
            this.runStartedAt = 0;
            this.lastBookmarkCount = 0;
        }

        extractRawTitle() {
            try {
                const metaTitle = document.querySelector('meta[name="title"]');
                let t = metaTitle ? metaTitle.content : bookId;
                if (!t || t === 'undefined') t = bookId;
                return String(t).replace(/[\\/:*?"<>|]/g, '-').trim() || bookId;
            } catch (e) { return bookId; }
        }

        detectPages() {
            try {
                const logicalLast = getLastDisplayPageLabel();
                if (logicalLast) return parseInt(logicalLast, 10) || 0;
                return parseInt(GM_getValue(`pages_${bookId}_${volume}`, 0)) || 0;
            } catch (e) { return 0; }
        }

        extractSourcePage(str) {
            str = String(str).trim();
            if (!str || str === '?') return null;
            try {
                const mapped = getSourcePageFromLabel(str);
                if (mapped !== null) return mapped;
            } catch (e) { }
            try {
                const uiInput = document.getElementById('txtPage');
                if (uiInput && uiInput.value) {
                    const uiPageInt = parseInt(uiInput.value.trim());
                    const urlPage = parseInt(new URLSearchParams(window.location.search).get('page'));
                    const reqPageInt = parseInt(str);
                    if (!isNaN(uiPageInt) && !isNaN(urlPage) && !isNaN(reqPageInt)) return reqPageInt + (urlPage - uiPageInt);
                }
            } catch (e) { }
            return parseInt(str) || null;
        }

        async init() {
            this.render();
            if (this.isPlusBook) {
                this.updateUIDerivedState();
                return; // Plus books abort completely
            }

            await WantedRegistry.load();
            this.isWanted = WantedRegistry.has(bookId);

            const [cloudReady, metaReady] = await Promise.all([
                probeCloudStatus(bookId),
                probeMetadataStatus(bookId)
            ]);
            this.isCloudReady = cloudReady;
            this.hasRemoteMeta = metaReady;
            this.updateUIDerivedState();

            const task = TaskManager.get(bookId);
            if (task && (task.state === 'queued' || task.state === 'pending')) {
                await this.execute();
            }
        }

        render() {
            const initialTo = this.totalPages || getLastDisplayPageLabel() || '?';
            this.container.innerHTML = `
                <div id="d4s-pill">
                    <div class="drag-handle" title="${this.title}"><span class="book-title">${this.title}</span></div>
                    <div class="val-group">
                        <input id="d4s-from" type="text" value="1" /><span>-</span><input id="d4s-to" type="text" value="${initialTo}" />
                    </div>
                    <button id="d4s-btn" class="action-btn" title="状态检查中...">
                        <div id="d4s-fill" class="action-progress"></div>
                        <svg id="d4s-icon" class="action-icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"></circle></svg>
                    </button>
                    <button id="d4s-expand-btn" style="display:none;"></button>
                </div>
                <div id="d4s-statusbar" class="is-open">
                    <div id="d4s-status-title" class="status-title">准备中</div>
                    <div id="d4s-status-copy" class="status-copy">我先看看这本书现在能不能直接下载。</div>
                    <div id="d4s-status-actions" class="status-actions">
                        <button id="d4s-request-btn" class="status-action-btn">我已上传，催维护</button>
                        <button id="d4s-contribute-btn" class="status-action-btn is-primary">手动贡献整本</button>
                    </div>
                </div>
            `;
            const toInput = this.container.querySelector('#d4s-to');
            toInput.onblur = () => {
                const val = parseInt(toInput.value);
                if (!isNaN(val) && val > this.totalPages) {
                    this.totalPages = val;
                    GM_setValue(`pages_${bookId}_${volume}`, val);
                }
            };
            this.container.querySelector('#d4s-btn').onclick = () => this.execute(false);
            this.container.querySelector('#d4s-contribute-btn').onclick = () => this.execute(true);
            this.container.querySelector('#d4s-request-btn').onclick = () => this.requestCloudUpdate();
            UIEngine.applyDrag(this.container, this.container.querySelector('.drag-handle'));
        }

        updateUIDerivedState() {
            const btn = this.container.querySelector('#d4s-btn');
            const icon = this.container.querySelector('#d4s-icon');
            const statusTitle = this.container.querySelector('#d4s-status-title');
            const statusCopy = this.container.querySelector('#d4s-status-copy');
            const statusBar = this.container.querySelector('#d4s-statusbar');
            const statusActions = this.container.querySelector('#d4s-status-actions');
            const contributeBtn = this.container.querySelector('#d4s-contribute-btn');
            const requestBtn = this.container.querySelector('#d4s-request-btn');

            icon.innerHTML = `<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line>`;
            statusBar.classList.add('is-open');
            statusActions.classList.remove('is-open');
            contributeBtn.disabled = false;
            requestBtn.disabled = false;
            contributeBtn.style.display = 'inline-flex';
            requestBtn.style.display = 'none';

            if (this.isPlusBook) {
                btn.className = 'action-btn d4s-ui-disabled';
                btn.disabled = true;
                icon.innerHTML = `<circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line>`;
                statusTitle.innerText = '当前书页暂不支持';
                statusCopy.innerText = '这是 Ebook+ 跳转页，先不在这里处理。';
            } else if (this.isCloudReady) {
                btn.className = 'action-btn d4s-ui-download';
                btn.disabled = false;
                btn.title = '这本书已入库，可直接下载';
                statusTitle.innerText = '现在可以直接下载';
                statusCopy.innerText = '输入你看到的页码，点右边就行。';
            } else if (this.isWanted) {
                btn.className = 'action-btn d4s-ui-download';
                btn.disabled = false;
                btn.title = '先下载你输入的范围';
                statusTitle.innerText = '先下载你要看的部分';
                statusCopy.innerText = '右边只下载你当前这段；想顺手帮大家，再点下面的“手动贡献整本”。';
                statusActions.classList.add('is-open');
                requestBtn.style.display = 'inline-flex';
                contributeBtn.style.display = 'inline-flex';
            } else {
                btn.className = 'action-btn d4s-ui-download';
                btn.disabled = false;
                btn.title = '这本书暂未入库，先本地下载';
                statusTitle.innerText = '先下载你要看的部分';
                statusCopy.innerText = '右边只下载你当前这段；你愿意的话，也可以手动贡献整本。';
                statusActions.classList.add('is-open');
                contributeBtn.style.display = 'inline-flex';
                requestBtn.style.display = 'inline-flex';
            }
        }

        updateProgress(current, total, msg) {
            const pct = (current / total) * 100;
            const elapsed = this.runStartedAt ? formatDurationMs(Date.now() - this.runStartedAt) : '';
            this.container.querySelector('#d4s-fill').style.height = `${pct}%`;
            this.container.querySelector('#d4s-status-title').innerText = msg;
            if (this.runMode === 'contribute') {
                this.container.querySelector('#d4s-status-copy').innerText = `正在处理第 ${current} / ${total} 页，已用时 ${elapsed}。整本做好后会先自动下载给你。`;
            } else {
                this.container.querySelector('#d4s-status-copy').innerText = `正在处理第 ${current} / ${total} 页，已用时 ${elapsed}。做好后会自动下载。`;
            }
            TaskManager.set(bookId, 'running', `${msg} (${current}/${total})`);
        }

        triggerTemporaryUI(iconSVG, colorVar, messageStr, copyStr = '') {
            const icon = this.container.querySelector('#d4s-icon');
            icon.innerHTML = iconSVG;
            if (colorVar) icon.style.stroke = `var(${colorVar})`;

            this.container.querySelector('#d4s-status-title').innerText = messageStr;
            this.container.querySelector('#d4s-status-copy').innerText = copyStr;
            this.container.querySelector('#d4s-fill').style.height = '0%';

            setTimeout(() => {
                if (this.isRunning) return;
                icon.style.stroke = '';
                this.updateUIDerivedState();
                this.container.querySelector('#d4s-btn').disabled = false;
                this.container.querySelector('#d4s-from').disabled = false;
                this.container.querySelector('#d4s-to').disabled = false;
                this.container.querySelector('#d4s-contribute-btn').disabled = false;
                this.container.querySelector('#d4s-request-btn').disabled = false;
            }, 3000);
        }

        async requestCloudUpdate() {
            if (this.isRunning || this.isPlusBook || this.isCloudReady) return;

            const requestBtn = this.container.querySelector('#d4s-request-btn');
            const contributeBtn = this.container.querySelector('#d4s-contribute-btn');
            requestBtn.disabled = true;
            contributeBtn.disabled = true;

            try {
                this.container.querySelector('#d4s-status-title').innerText = '正在提醒维护者';
                this.container.querySelector('#d4s-status-copy').innerText = '我会把这本书发给维护者。这个按钮只给已经上传过整本的人用。';

                const metaPayload = buildBookMetadataPayload(this.title);
                await postJobNotice({
                    type: 'nudge',
                    bookId,
                    title: this.title,
                    volume,
                    sourcePageCount: metaPayload.sourcePageCount,
                    outlineStatus: metaPayload.outlineStatus,
                    outlineCount: metaPayload.outline.length,
                    isWanted: this.isWanted
                });

                this.triggerTemporaryUI(`<polyline points="20 6 9 17 4 12"></polyline>`, '--success-col', '已经提醒维护者', '消息已经发出，接下来等维护者处理就行。');
            } catch (err) {
                console.error("D4S Request Notice Failed:", err);
                this.triggerTemporaryUI(`<circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line>`, '--err-col', '这次没发出去', '稍后再点一次就行。');
            }
        }

        async contributeMetadataOnly() {
            this.container.querySelector('#d4s-status-title').innerText = '正在补齐目录信息';
            this.container.querySelector('#d4s-status-copy').innerText = 'PDF 已经给你了，我顺手把这本书的目录一起补上。';
            TaskManager.set(bookId, 'running', '补充书库信息...');

            const metaPayload = buildBookMetadataPayload(this.title);
            const metaBlob = new Blob([JSON.stringify(metaPayload, null, 2)], { type: 'application/json' });
            const metaLink = await uploadTransientBlob(metaBlob, `${bookId}-0.dat`);

            this.container.querySelector('#d4s-status-copy').innerText = '目录信息已经补好，正在通知维护者。';
            TaskManager.set(bookId, 'running', '发送入库通知...');

            await postJobNotice({
                type: 'meta_upload',
                bookId,
                title: this.title,
                volume,
                metaLink,
                outlineStatus: metaPayload.outlineStatus,
                outlineCount: metaPayload.outline.length,
                sourcePageCount: metaPayload.sourcePageCount,
                pdf: {
                    mode: 'none',
                    url: '',
                    label: 'already in cloud'
                }
            });

            try {
                await PendingRegistry.publishRecord({
                    bookId,
                    title: this.title,
                    volume,
                    pdfLink: 'already in cloud',
                    metaLink,
                    outlineStatus: metaPayload.outlineStatus,
                    outlineCount: metaPayload.outline.length,
                    sourcePageCount: metaPayload.sourcePageCount
                });
            } catch (registryErr) {
                console.warn('D4S Pending registry publish failed:', registryErr);
            }

            this.hasRemoteMeta = true;
        }

        async execute(shouldContribute = false) {
            if (this.isRunning || this.isPlusBook) return;
            shouldContribute = !!shouldContribute && !this.isCloudReady;
            this.isRunning = true;
            this.runMode = shouldContribute ? 'contribute' : (this.isCloudReady ? 'cloud' : 'local');
            this.runStartedAt = Date.now();
            this.lastBookmarkCount = 0;
            let partialWarning = '';

            const fStr = this.container.querySelector('#d4s-from').value;
            const tStr = this.container.querySelector('#d4s-to').value;
            let logicalFrom = String(fStr || '').trim() || '1';
            let logicalTo = String(tStr || '').trim() || logicalFrom;
            let sourceFrom = this.extractSourcePage(fStr) || 1;
            let sourceTo = this.extractSourcePage(tStr === '?' || tStr === '' ? fStr : tStr) || sourceFrom;
            const needsSwap = sourceFrom > sourceTo;

            if (needsSwap) {
                [sourceFrom, sourceTo] = [sourceTo, sourceFrom];
                [logicalFrom, logicalTo] = [logicalTo, logicalFrom];
            }

            if (!this.isCloudReady && shouldContribute) {
                const fullSourceCount = getTotalSourcePageCount();
                const boundaries = getContributionBoundaryLabels();
                sourceFrom = 1;
                sourceTo = fullSourceCount || sourceTo;
                logicalFrom = boundaries.first;
                logicalTo = boundaries.last;
                this.container.querySelector('#d4s-status-title').innerText = '正在准备整本';
                this.container.querySelector('#d4s-status-copy').innerText = '这次会整本处理。先把 PDF 给你，再继续把它补进书库。';
            }

            const btn = this.container.querySelector('#d4s-btn');
            btn.disabled = true;
            this.container.querySelector('#d4s-contribute-btn').disabled = true;

            this.container.querySelector('#d4s-from').disabled = true;
            this.container.querySelector('#d4s-to').disabled = true;

            TaskManager.set(bookId, 'running', `开启任务...`);

            try {
                let success = false;
                if (this.isCloudReady) {
                    success = await this.pipelineDownload(sourceFrom, sourceTo, logicalFrom, logicalTo);
                    if (success && !this.hasRemoteMeta) {
                        try {
                            await this.contributeMetadataOnly();
                        } catch (metaErr) {
                            console.warn("D4S Metadata Top-up Failed:", metaErr);
                            partialWarning = `PDF 已下载，但书库补充失败：${metaErr.message || '未知错误'}`;
                        }
                    }
                    if (!success) {
                        console.warn("Cloud fallback to rendering.");
                        TaskManager.set(bookId, 'running', '自动转入绘制模式');
                        this.runMode = 'local';
                        success = await this.pipelineRender(sourceFrom, sourceTo, logicalFrom, logicalTo, false);
                    }
                } else {
                    success = await this.pipelineRender(sourceFrom, sourceTo, logicalFrom, logicalTo, shouldContribute);
                }

                if (!success) throw new Error("发生致命中止");

                const elapsed = formatDurationMs(Date.now() - this.runStartedAt);
                const bookmarkSummary = this.lastBookmarkCount > 0
                    ? `已写入目录书签（${this.lastBookmarkCount} 条）`
                    : '这次没有可写入的目录书签';

                if (partialWarning) {
                    TaskManager.set(bookId, 'failed', partialWarning);
                    this.triggerTemporaryUI(
                        `<circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line>`,
                        '--warn-col',
                        'PDF 已经好了',
                        `PDF 已经给你了，${bookmarkSummary}，用时 ${elapsed}；只是后台那一步这次没发出去。`
                    );
                    return;
                }

                if (shouldContribute) TaskManager.set(bookId, 'done', '整本已提交，等待仓库同步');
                else TaskManager.clear(bookId);
                this.triggerTemporaryUI(
                    `<polyline points="20 6 9 17 4 12"></polyline>`,
                    '--success-col',
                    '已经好了',
                    shouldContribute
                        ? `整本已经处理好，${bookmarkSummary}，用时 ${elapsed}。`
                        : `PDF 已经准备好，${bookmarkSummary}，用时 ${elapsed}。`
                );

            } catch (err) {
                console.error("D4S Export Task Failed:", err);
                TaskManager.set(bookId, 'failed', err.message || '内部运行错误');
                this.triggerTemporaryUI(
                    `<circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line>`,
                    '--err-col',
                    '这次没做完',
                    '你可以直接再试一次；如果总失败，再看控制台报错。'
                );
            } finally {
                this.isRunning = false;
                this.runMode = 'idle';
            }
        }

        async pipelineDownload(f, t, logicalFrom, logicalTo) {
            const { PDFDocument } = window.PDFLib;
            const finalPdf = await PDFDocument.create();
            const total = t - f + 1;

            for (let i = f; i <= t; i++) {
                this.updateProgress(i - f + 1, total, '合并远端文件');
                let buffer = null;
                const refs = buildDistributionRefs(`${bookId}/${i}.dat`, false);
                for (const ref of refs) {
                    try {
                        buffer = await gmFetch(ref.url, 'arraybuffer');
                        break;
                    } catch (e) { }
                }
                if (!buffer) return false;
                const donor = await PDFDocument.load(buffer);
                const [copied] = await finalPdf.copyPages(donor, [0]);
                finalPdf.addPage(copied);
            }

            const metaPayload = await resolveBookMetadataPayload(this.title);
            this.lastBookmarkCount = await attachOutlineBookmarks(finalPdf, metaPayload, f, t);
            const bytes = await finalPdf.save();
            const blob = new Blob([bytes], { type: "application/pdf" });
            const lnk = document.createElement('a');
            lnk.href = URL.createObjectURL(blob);
            lnk.download = `${this.title}_[${logicalFrom}-${logicalTo}].pdf`;
            lnk.click();
            return true;
        }

        async pipelineRender(f, t, logicalFrom, logicalTo, shouldContribute) {
            const canvas = document.createElement('canvas');
            const ctx = canvas.getContext('2d', { willReadFrequently: true });
            const jsPDF = window.jspdf.jsPDF;
            const { PDFDocument } = window.PDFLib;
            let innerJsPdf = null;
            const total = t - f + 1;

            for (let i = f; i <= t; i++) {
                this.updateProgress(i - f + 1, total, '处理中...');
                const volPath = volume ? `/${volume}` : '';
                const urls = [
                    `https://a.digi4school.at/ebook/${bookId}${volPath}/${i}.svg`,
                    `https://a.digi4school.at/ebook/${bookId}${volPath}/${i}/${i}.svg`,
                    `https://a.digi4school.at/ebook/${bookId}/${i}.svg`,
                    `https://a.digi4school.at/ebook/${bookId}/${i}/${i}.svg`
                ];

                let svgText, baseUrl;
                for (let u of urls) {
                    try {
                        svgText = await gmFetch(u, 'text');
                        baseUrl = u.substring(0, u.lastIndexOf('/') + 1);
                        break;
                    } catch (e) { }
                }
                if (!svgText) throw new Error(`缺乏关键渲染物料: ${i}`);

                const parser = new DOMParser();
                const doc = parser.parseFromString(svgText, "image/svg+xml");

                const imgPromises = Array.from(doc.querySelectorAll('image')).map(async (imgNode) => {
                    let href = imgNode.getAttribute('href') || imgNode.getAttribute('xlink:href');
                    if (href && !href.startsWith('data:')) {
                        try {
                            const absUrl = new URL(href, baseUrl).href;
                            const buf = await gmFetch(absUrl, 'arraybuffer');
                            const b64 = await new Promise(res => {
                                const r = new FileReader();
                                r.onloadend = () => res(r.result);
                                r.readAsDataURL(new Blob([buf]));
                            });
                            imgNode.setAttribute('href', b64);
                            imgNode.setAttribute('xlink:href', b64);
                        } catch (e) { }
                    }
                });
                await Promise.all(imgPromises);

                let root = doc.documentElement;
                if (!root.getAttribute('width') && root.getAttribute('viewBox')) {
                    let pts = root.getAttribute('viewBox').split(/\s+/);
                    if (pts.length >= 4) { root.setAttribute('width', pts[2] + 'px'); root.setAttribute('height', pts[3] + 'px'); }
                }

                const objUrl = 'data:image/svg+xml;base64,' + btoa(unescape(encodeURIComponent(new XMLSerializer().serializeToString(doc))));

                await new Promise((resolve, reject) => {
                    const img = new Image();
                    img.onload = () => {
                        const w = img.naturalWidth || 909; const h = img.naturalHeight || 1286;
                        canvas.width = w * CONFIG.HD_SCALE; canvas.height = h * CONFIG.HD_SCALE;
                        ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, canvas.width, canvas.height);
                        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
                        const imgData = canvas.toDataURL('image/jpeg', 0.95);

                        if (!innerJsPdf) innerJsPdf = new jsPDF({ unit: 'px', format: [w, h], orientation: h > w ? 'portrait' : 'landscape', compress: true });
                        else innerJsPdf.addPage([w, h], h > w ? 'portrait' : 'landscape');

                        innerJsPdf.addImage(imgData, 'JPEG', 0, 0, w, h);
                        resolve();
                    };
                    img.onerror = () => reject(new Error(`画布解析断裂: ${i}`));
                    img.src = objUrl;
                });
            }

            const rawBytes = innerJsPdf.output('arraybuffer');
            const finalPdf = await PDFDocument.load(rawBytes);
            const metaPayload = await resolveBookMetadataPayload(this.title);
            this.lastBookmarkCount = await attachOutlineBookmarks(finalPdf, metaPayload, f, t);
            const outBytes = await finalPdf.save();

            const blob = new Blob([outBytes], { type: "application/pdf" });
            const lnk = document.createElement('a');
            lnk.href = URL.createObjectURL(blob);
            lnk.download = `${this.title}_[${logicalFrom}-${logicalTo}].pdf`;
            lnk.click();

            if (!shouldContribute) return true;

            this.container.querySelector('#d4s-status-title').innerText = '正在继续处理';
            this.container.querySelector('#d4s-status-copy').innerText = 'PDF 已经给你了，我继续把这本书补进书库。';
            TaskManager.set(bookId, 'running', `补进书库...`);

            const metaBlob = new Blob([JSON.stringify(metaPayload, null, 2)], { type: 'application/json' });

            const pdfUpload = await uploadPdfPayload(
                blob,
                `${bookId}-${this.title}.pdf`,
                this.title,
                (currentPart, totalParts) => {
                    this.container.querySelector('#d4s-status-copy').innerText = `PDF 比较大，正在上传第 ${currentPart}/${totalParts} 段。`;
                    TaskManager.set(bookId, 'running', `提交 PDF 分段 ${currentPart}/${totalParts}`);
                }
            );
            this.container.querySelector('#d4s-status-copy').innerText = 'PDF 已经交出去了，正在继续补齐目录信息。';
            TaskManager.set(bookId, 'running', `补充书库信息...`);

            const metaLink = await uploadTransientBlob(metaBlob, `${bookId}-0.dat`);
            this.container.querySelector('#d4s-status-copy').innerText = '这本书已经补好了，正在通知维护者。';
            TaskManager.set(bookId, 'running', `发送入库通知...`);

            await postJobNotice({
                type: 'full_upload',
                bookId,
                title: this.title,
                volume,
                metaLink,
                outlineStatus: metaPayload.outlineStatus,
                outlineCount: metaPayload.outline.length,
                sourcePageCount: metaPayload.sourcePageCount,
                pdf: {
                    mode: pdfUpload.mode === 'parts' ? 'manifest' : 'direct',
                    url: pdfUpload.link || '',
                    label: pdfUpload.label || '',
                    parts: Array.isArray(pdfUpload.parts) ? pdfUpload.parts : []
                }
            });

            try {
                await PendingRegistry.publishRecord({
                    bookId,
                    title: this.title,
                    volume,
                    pdfLink: pdfUpload.label,
                    metaLink,
                    outlineStatus: metaPayload.outlineStatus,
                    outlineCount: metaPayload.outline.length,
                    sourcePageCount: metaPayload.sourcePageCount
                });
            } catch (registryErr) {
                console.warn('D4S Pending registry publish failed:', registryErr);
            }

            return true;
        }
    }

    // ==========================================
    // 7. BOOTSTRAP
    // ==========================================
    function safeBoot() {
        if (!window.jspdf?.jsPDF || !window.PDFLib?.PDFDocument) return;
        if (document.getElementById('d4s-container')) return;
        if (!document.body) {
            setTimeout(safeBoot, 500);
            return;
        }

        if (!UIEngine.initCSS()) {
            setTimeout(safeBoot, 500);
            return;
        }
        const container = UIEngine.createContainer();
        document.body.appendChild(container);

        if (isOverview) new OverviewManager(container).init();
        else if (isEbook) new EbookManager(container).init();
    }

    if (document.readyState === 'complete') safeBoot();
    else window.addEventListener('load', () => setTimeout(safeBoot, 800));

})();
