import { createI18n } from 'vue-i18n'
import zhCN from './zh-CN.json'
import enUS from './en-US.json'

const supportedLocales = ['zh-CN', 'en-US']

function getDefaultLocale() {
  const saved = localStorage.getItem('xc-buddy-locale')
  if (supportedLocales.includes(saved)) return saved

  const languages = navigator.languages?.length ? navigator.languages : [navigator.language]
  const preferred = languages.find((language) => language?.toLowerCase().startsWith('zh'))
  return preferred ? 'zh-CN' : 'en-US'
}

export function setLocale(locale) {
  if (!supportedLocales.includes(locale)) return
  i18n.global.locale.value = locale
  localStorage.setItem('xc-buddy-locale', locale)
  document.documentElement.lang = locale
}

const i18n = createI18n({
  legacy: false,
  locale: getDefaultLocale(),
  fallbackLocale: 'en-US',
  messages: {
    'zh-CN': zhCN,
    'en-US': enUS,
  },
})

document.documentElement.lang = i18n.global.locale.value

export default i18n
