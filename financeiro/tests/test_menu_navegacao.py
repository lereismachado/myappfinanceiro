from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from financeiro.models import Cartao, Categoria, Conta, Pessoa


class MenuNavegacaoTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_user(username="menu", password="senha")
        self.client.force_login(self.usuario)
        self.conta = Conta.objects.create(usuario=self.usuario, nome="Conta principal")
        self.cartao = Cartao.objects.create(usuario=self.usuario, nome="Cartão principal", dia_vencimento=10)
        self.categoria = Categoria.objects.create(usuario=self.usuario, nome="Alimentação")
        self.pessoa = Pessoa.objects.create(usuario=self.usuario, nome="Fornecedor")

    def test_menu_reune_cadastros_e_preserva_destinos(self):
        resposta = self.client.get(reverse("dashboard"))

        self.assertContains(resposta, '<details class="nav-group">', html=False)
        self.assertContains(resposta, "<span>Cadastros</span>", html=False)
        self.assertContains(resposta, f'href="{reverse("cartoes")}"', html=False)
        self.assertContains(resposta, "<span>Cartões de crédito</span>", html=False)
        self.assertContains(resposta, f'href="{reverse("contas")}"', html=False)
        self.assertContains(resposta, "<span>Contas financeiras</span>", html=False)
        self.assertContains(resposta, f'href="{reverse("categorias")}"', html=False)
        self.assertContains(resposta, "<span>Categorias</span>", html=False)
        self.assertContains(resposta, f'href="{reverse("pessoas")}"', html=False)
        self.assertContains(resposta, "<span>Pessoas</span>", html=False)
        self.assertContains(resposta, f'href="{reverse("regras_categoria")}"', html=False)
        self.assertContains(resposta, "<span>Regras</span>", html=False)
        self.assertNotContains(resposta, "Cadastro de cartões")

    def test_cadastros_fica_fechado_fora_da_secao(self):
        resposta = self.client.get(reverse("dashboard"))

        self.assertContains(resposta, '<details class="nav-group">', html=False)
        self.assertNotContains(resposta, '<details class="nav-group active" open>', html=False)

    def test_cadastros_abre_e_destaca_listagens(self):
        casos = (
            ("cartoes", "Cartões de crédito"),
            ("contas", "Contas financeiras"),
            ("categorias", "Categorias"),
            ("pessoas", "Pessoas"),
            ("regras_categoria", "Regras"),
        )

        for nome_url, rotulo in casos:
            with self.subTest(nome_url=nome_url):
                resposta = self.client.get(reverse(nome_url))
                self.assertContains(resposta, '<details class="nav-group active" open>', html=False)
                self.assertContains(resposta, f'class="active" aria-current="page"><svg', html=False)
                self.assertContains(resposta, f"<span>{rotulo}</span>", html=False)

    def test_cadastros_abre_em_inclusao_e_edicao(self):
        casos = (
            reverse("conta_nova"),
            reverse("conta_editar", args=[self.conta.pk]),
            reverse("cartao_novo"),
            reverse("cartao_editar", args=[self.cartao.pk]),
            reverse("categoria_nova"),
            reverse("categoria_editar", args=[self.categoria.pk]),
            reverse("pessoa_nova"),
            reverse("pessoa_editar", args=[self.pessoa.pk]),
            reverse("regra_categoria_nova"),
        )

        for url in casos:
            with self.subTest(url=url):
                resposta = self.client.get(url)
                self.assertEqual(resposta.status_code, 200)
                self.assertContains(resposta, '<details class="nav-group active" open>', html=False)

    def test_menu_e_logout_possuem_icones_decorativos(self):
        resposta = self.client.get(reverse("dashboard"))

        self.assertGreaterEqual(resposta.content.count(b'<svg aria-hidden="true"'), 10)
        self.assertContains(resposta, '<button type="submit"><svg aria-hidden="true"', html=False)

    def test_versao_aparece_abaixo_do_logout_e_e_ocultada_no_layout_movel(self):
        resposta = self.client.get(reverse("dashboard"))
        conteudo = resposta.content.decode()

        self.assertContains(resposta, '<div class="app-version">v2.10.25</div>', html=False)
        self.assertGreater(conteudo.index('<div class="app-version">'), conteudo.index('class="nav-logout"'))
        self.assertContains(resposta, "aside p,.aside-foot,.app-version{display:none}", html=False)

    def test_links_do_menu_acionam_o_loading_existente(self):
        resposta = self.client.get(reverse("dashboard"))

        self.assertContains(resposta, "event.target.closest('[data-loading-link], nav a')", html=False)
        self.assertContains(
            resposta,
            "showPageLoading(loadingLink.dataset.loadingMessage || 'Carregando...')",
            html=False,
        )
        self.assertContains(resposta, "window.addEventListener('pageshow'", html=False)

    def test_modal_remoto_abre_com_loading_e_permite_tentar_novamente(self):
        resposta = self.client.get(reverse("dashboard"))
        conteudo = resposta.content.decode()

        self.assertContains(resposta, "Carregando formulário...")
        self.assertContains(resposta, "editorBody.setAttribute('aria-busy', 'true')", html=False)
        self.assertContains(resposta, 'class="modal-loading-state" role="status" aria-live="polite"', html=False)
        self.assertContains(resposta, 'data-retry-modal', html=False)
        self.assertContains(resposta, "if (!response.ok) throw new Error", html=False)
        self.assertContains(resposta, "modalRequestController?.abort()", html=False)
        self.assertContains(resposta, "let currentModalUrl = '';", html=False)
        self.assertContains(resposta, "openModal(currentModalUrl, currentModalTitle)", html=False)
        self.assertContains(resposta, "if (trigger && !editor.contains(trigger))", html=False)
        self.assertNotContains(resposta, "editor.dataset.modalUrl", html=False)
        abertura = conteudo.index("renderModalLoading(currentModalTitle)")
        requisicao = conteudo.index("const response = await fetch(url", abertura)
        self.assertLess(abertura, requisicao)

    def test_menu_horizontal_tem_controles_e_recolhe_cadastros(self):
        resposta = self.client.get(reverse("contas"))

        self.assertContains(resposta, 'data-nav-scroll="left" hidden', html=False)
        self.assertContains(resposta, 'data-nav-scroll="right" hidden', html=False)
        self.assertContains(resposta, "window.matchMedia('(max-width: 1024px)')", html=False)
        self.assertContains(resposta, "if (horizontalNavQuery.matches) navGroup.open = false;", html=False)
        self.assertContains(resposta, "event.target.closest('.nav-submenu a')", html=False)
