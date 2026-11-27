import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import test from 'node:test'
import { transformSync } from 'esbuild'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const source = readFileSync(new URL('../../public/elements/BoneTaskCard.jsx', import.meta.url), 'utf8')
const compiled = transformSync(source, { loader: 'jsx', jsx: 'automatic', format: 'cjs' }).code
const loadCard = new Function('module', 'require', 'props', 'callAction', 'submitElement', 'cancelElement', compiled)
const require = createRequire(import.meta.url)
const unavailable = () => { throw new Error('Rendering must not invoke an action') }

for (const reason of ['Preview is busy.', 'Preview preparation timed out.', 'Preview source could not be verified.']) {
  for (const confirmed of [false, true]) {
    test(`candidate ${confirmed ? 'summary' : 'list'} shows ${reason}`, () => {
      const candidates = [1, 2, 3].map(rank => ({
        id: String(rank), rank, previewState: 'unavailable', previewMessage: reason
      }))
      const module = { exports: {} }
      loadCard(module, require, { kind: 'candidates', candidates, selectedId: '2',
        confirmationState: confirmed ? 'confirmed' : 'ready' }, unavailable, unavailable, unavailable)
      const html = renderToStaticMarkup(React.createElement(module.exports.default))
      assert.equal(html.split(`<span>${reason}</span>`).length - 1, confirmed ? 1 : 3)
      assert.doesNotMatch(html, /Preparing preview/)
    })
  }
}
