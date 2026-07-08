<?php
/**
 * @var \App\View\AppView $this
 * @var iterable<\App\Model\Entity\Article> $articles
 */
?>
<ul>
    <?php foreach ($articles as $article): ?>
        <li>
            <?= $this->Html->link($article->title, ['action' => 'view', $article->slug]) ?>
            <span><?= h($article->author_name) ?></span>
        </li>
    <?php endforeach; ?>
</ul>
