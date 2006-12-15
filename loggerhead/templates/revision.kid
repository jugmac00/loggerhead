<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:py="http://purl.org/kid/ns#"
    py:extends="'master.kid'">
<head>
    <meta content="text/html; charset=utf-8" http-equiv="Content-Type" py:replace="''"/>
    <title> ${branch_name} : revision ${change.revno} </title>
    
    <span py:def="file_link(filename)">
        <a href="${tg.url([ '/annotate', revid ], path=filename)}" title="Annotate ${filename}">${filename}</a>
    </span>
</head>

<body>

${navbar()}

<h1> <span class="branch-name">${branch_name}</span> : revision ${change.revno}
	<div class="links">
	    <div> <b>&#8594;</b> <a href="${tg.url([ '/files', revid ])}">browse files</a> </div>
	    <div> <b>&#8594;</b> <a href="${tg.url('/changes', start_revid=revid)}">view branch changes</a> </div>
	</div>
</h1>
 
<div class="revision-info">
    <table>
        <tr>
            <th class="author">committed by:</th>
            <td class="author"> ${util.hide_email(change.author)} </td>
        </tr>
        <tr>
            <th class="date">date:</th>
            <td class="date"> ${change.date.strftime('%d %b %Y %H:%M')} </td>
        </tr>

        <tr py:if="len(change.merge_points) > 0">
            <th class="children"> merged in: </th>
            <td class="children">
                <span py:for="child in change.merge_points">
                    ${revlink_path(child.revid, child.revid, '(' + child.revno + util.if_present(' %s', child.branch_nick) + ')', None)} &nbsp; 
                </span>
            </td>
        </tr>
        <tr py:if="len(change.parents) > 1">
        	<th class="parents"> merged from: </th>
        	<td class="parents">
        	    <span py:for="parent in change.parents"><span py:if="parent.revid != change.parents[0].revid">
        	        ${revlink_path(parent.revid, parent.revid, '(' + parent.revno + util.if_present(' %s', parent.branch_nick) + ')', None)} &nbsp;
        	    </span></span>
        	</td>
        </tr>

        <tr>
            <th class="description">description:</th>
            <td class="description"><span py:for="line in change.comment_clean">${XML(line)} <br /></span> </td>
        </tr>
        
        <tr class="divider"> <th></th> <td></td> </tr>
        
        <tr py:if="change.changes.added">
            <th class="files"> files added: </th>
            <td class="files"> <span py:for="filename in change.changes.added" class="filename">${file_link(filename)} <br /></span> </td>
        </tr>
        <tr py:if="change.changes.removed">
            <th class="files"> files removed: </th>
            <td class="files"> <span py:for="filename in change.changes.removed" class="filename">${file_link(filename)} <br /></span> </td>
        </tr>
        <tr py:if="change.changes.renamed">
            <th class="files"> files renamed: </th>
            <td class="files"> <span py:for="old_filename, new_filename in change.changes.renamed" class="filename">${file_link(old_filename)} => ${file_link(new_filename)}<br /></span> </td>
        </tr>
        <tr py:if="change.changes.modified">
            <th class="files"> files modified: </th>
            <td class="files">
                <span py:for="item in change.changes.modified">
                    <span class="filename">${file_link(item.filename)}</span> &nbsp; <a href="#${item.filename}" class="jump">(jump to diff)</a><br />
                </span>
            </td>
        </tr>
    </table>
</div>

<div class="diff" py:if="change.changes.modified">
    <table py:for="item in change.changes.modified" class="diff-block">
        <tr><th class="filename"> <a href="${tg.url([ '/annotate', change.revid ], path=item.filename)}" name="${item.filename}">${item.filename}</a> </th></tr>
        <tr><td>
            <table py:for="chunk in item.chunks" class="diff-chunk">
                <tr> <th class="lineno">old</th> <th class="lineno">new</th> <th></th> </tr>
                <tr py:for="line in chunk.diff">
                    <td class="lineno">${line.old_lineno}</td>
                    <td class="lineno">${line.new_lineno}</td>
                    <td class="${line.type} text">${XML(line.line)}</td>
                </tr>
            </table>
        </td></tr>
    </table>
</div>

<div class="bar">
    <table>
        <tr>
        	<td class="buttons">
            	<a py:if="navigation.prev_page_revid" href="${navigation.prev_page_url}"> &lt; revision ${history.get_revno(navigation.prev_page_revid)} </a>
	 		</td>
 			<td class="rbuttons" align="right">
            	<a py:if="navigation.next_page_revid" href="${navigation.next_page_url}"> revision ${history.get_revno(navigation.next_page_revid)} &gt; </a>
 			</td>
 		</tr>
 	</table>
</div>

</body>
</html>
